"""Optional Claude analysis: every gate lives here, in a fixed order, BEFORE any network call.

Order of checks for ``analyze``:
  1. enabled (default OFF)   2. API key present   3. model has a known price
  4. message exists and is not a conflicting import
  5. already analysed with this model/prompt -> return the stored analysis, send NOTHING
  6. input size limit        7. per-request and daily cost guards
  8. explicit consent matching the preview (confirm + model + accepted cost)
Only then is the provider called, OUTSIDE any database transaction and without retries.

A failed request never creates an analysis; it leaves only a usage row and an audit event
(no content). A successful one stores a separate ``llm`` analysis row, its usage row and an
audit event in ONE transaction. Existing cases, their analysis and their corrections are never
touched: a case keeps the analysis it was created with.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from ai_ops_hub.analysis.claude import pricing
from ai_ops_hub.analysis.claude.analyzer import ANALYZER_NAME, CallInfo, ClaudeAnalyzer, make_client
from ai_ops_hub.analysis.claude.models import (
    ClaudeAnalyzeResponse,
    ClaudeConsent,
    ClaudePreview,
    ClaudeStatus,
    LlmUsage,
)
from ai_ops_hub.analysis.claude.prompt import PROMPT_VERSION
from ai_ops_hub.analysis.compare import AnalysisComparison, compare
from ai_ops_hub.analysis.schemas import AnalysisInput, StoredAnalysis
from ai_ops_hub.config import OFFICIAL_CLAUDE_BASE_URL, Settings
from ai_ops_hub.db.engine import for_writes
from ai_ops_hub.db.models import LlmCallRecord, MessageRecord
from ai_ops_hub.db.repositories import (
    AnalysisRepository,
    AuditRepository,
    CaseRepository,
    LlmCallRepository,
    MessageRepository,
)
from ai_ops_hub.errors import (
    AnalysisNotFoundError,
    ClaudeBadResponseError,
    ClaudeConsentRequiredError,
    ClaudeCostGuardError,
    ClaudeDisabledError,
    ClaudeError,
    ClaudeInProgressError,
    ClaudeProviderError,
    ClaudeInputTooLargeError,
    ClaudeNotConfiguredError,
    MessageNotFoundError,
)
from ai_ops_hub.schemas import Actor, AnalysisMethod, AuditEventType, ImportStatus
from ai_ops_hub.services.analysis_service import _stored
from ai_ops_hub.services.import_service import utc_now

log = logging.getLogger("ai_ops_hub.claude")

ClaudeClientFactory = Callable[[str, float], object]

IN_FLIGHT_WINDOW = timedelta(minutes=10)  # a reservation younger than this blocks an identical second request


def _host(url: str) -> str:
    return urlsplit(url).hostname or url


class _AlreadyAnalyzed(Exception):
    pass


class ClaudeService:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        clock: Callable[[], datetime] = utc_now,
        client_factory: ClaudeClientFactory = make_client,
    ) -> None:
        self._engine = engine
        self._write_engine = for_writes(engine)
        self._s = settings
        self._clock = clock
        self._client_factory = client_factory

    # --- gates --------------------------------------------------------------------------

    @property
    def _key(self) -> str | None:
        secret = self._s.anthropic_api_key
        value = secret.get_secret_value().strip() if secret else ""
        return value or None

    def _blocked(self) -> str | None:
        if not self._s.claude_enabled:
            return "Claude API analysis is disabled (default). Set AIOPS_CLAUDE_ENABLED=true to allow it."
        if self._key is None:
            return "No API key: set ANTHROPIC_API_KEY in your environment or in the ignored .env file."
        if not pricing.known_model(self._s.claude_model):
            return f"Model {self._s.claude_model!r} has no known price, so its cost cannot be estimated."
        return None

    def _raise_if_blocked(self) -> None:
        if not self._s.claude_enabled:
            raise ClaudeDisabledError("Claude API analysis is disabled; enable it in the configuration first")
        if self._key is None:
            raise ClaudeNotConfiguredError("no ANTHROPIC_API_KEY is configured")
        if not pricing.known_model(self._s.claude_model):
            raise ClaudeCostGuardError(
                f"model {self._s.claude_model!r} has no known price, so its cost cannot be estimated"
            )

    def _spent_24h(self, session: Session) -> float:
        return LlmCallRepository(session).spent_since(self._clock() - timedelta(hours=24))

    # --- reads --------------------------------------------------------------------------

    def status(self) -> ClaudeStatus:
        s = self._s
        price = pricing.PRICES.get(s.claude_model)
        with Session(self._engine, expire_on_commit=False) as session:
            spent = self._spent_24h(session)
        reason = self._blocked()
        return ClaudeStatus(
            enabled=s.claude_enabled,
            key_configured=self._key is not None,
            model=s.claude_model,
            endpoint_host=_host(s.claude_base_url),
            custom_endpoint=s.claude_base_url != OFFICIAL_CLAUDE_BASE_URL,
            model_has_known_price=price is not None,
            input_usd_per_mtok=float(price.input_per_mtok) if price else None,
            output_usd_per_mtok=float(price.output_per_mtok) if price else None,
            prices_verified_on=pricing.PRICES_VERIFIED_ON,
            max_output_tokens=s.claude_max_output_tokens,
            max_input_chars=s.claude_max_input_chars,
            max_cost_usd_per_request=s.claude_max_cost_usd_per_request,
            daily_budget_usd=s.claude_daily_budget_usd,
            spent_last_24h_usd=round(spent, 6),
            can_analyze=reason is None,
            blocked_reason=reason,
        )

    def preview(self, message_id: int) -> ClaudePreview:
        """Local, free, no network: what would be sent, the estimate, and whether it is allowed."""
        s = self._s
        with Session(self._engine, expire_on_commit=False) as session:
            msg = self._message(session, message_id)
            existing = AnalysisRepository(session).get_for(message_id, ANALYZER_NAME, self._version())
            spent = self._spent_24h(session)
            subject_chars, body_chars = len(msg.subject or ""), len(msg.body_text)
            fingerprint = msg.content_fingerprint
        chars = subject_chars + body_chars
        known = pricing.known_model(s.claude_model)
        tokens, cost = pricing.worst_case_cost_usd(s.claude_model, chars, s.claude_max_output_tokens) if known else (
            pricing.estimate_input_tokens(chars), Decimal(0))
        reason = self._blocked()
        if reason is None and existing is None:
            if chars > s.claude_max_input_chars:
                reason = f"The message has {chars} characters; the limit for Claude analysis is {s.claude_max_input_chars}."
            elif float(cost) > s.claude_max_cost_usd_per_request:
                reason = (f"Estimated worst-case cost ${float(cost):.4f} exceeds the per-request limit "
                          f"${s.claude_max_cost_usd_per_request:.4f}.")
            elif spent + float(cost) > s.claude_daily_budget_usd:
                reason = (f"The local 24-hour budget (${s.claude_daily_budget_usd:.2f}, spent so far "
                          f"${spent:.4f}) would be exceeded.")
        return ClaudePreview(
            message_id=message_id,
            enabled=s.claude_enabled,
            key_configured=self._key is not None,
            model=s.claude_model,
            endpoint_host=_host(s.claude_base_url),
            custom_endpoint=s.claude_base_url != OFFICIAL_CLAUDE_BASE_URL,
            content_fingerprint=fingerprint,
            subject_chars=subject_chars,
            body_chars=body_chars,
            estimated_input_tokens=tokens,
            max_output_tokens=s.claude_max_output_tokens,
            estimated_max_cost_usd=float(cost),
            already_analyzed=existing is not None,
            existing_analysis_id=existing.id if existing else None,
            can_send=reason is None and existing is None,
            blocked_reason=reason,
        )

    def usage_for_message(self, message_id: int) -> list[LlmUsage]:
        with Session(self._engine, expire_on_commit=False) as session:
            self._message(session, message_id)
            return [LlmUsage.model_validate(r) for r in LlmCallRepository(session).for_message(message_id)]

    def compare(self, message_id: int, analysis_a: int | None = None, analysis_b: int | None = None) -> AnalysisComparison:
        """Read-only. Default: the latest rule-based analysis against the latest LLM analysis."""
        with Session(self._engine, expire_on_commit=False) as session:
            self._message(session, message_id)
            repo = AnalysisRepository(session)

            def pick(analysis_id: int | None, method: AnalysisMethod) -> StoredAnalysis:
                rec = repo.get(analysis_id) if analysis_id is not None else repo.latest_for_message_by_method(
                    message_id, method.value)
                if rec is None or rec.message_id != message_id:
                    raise AnalysisNotFoundError(
                        f"no {method.value} analysis found for message {message_id}"
                        if analysis_id is None else f"analysis {analysis_id} does not belong to message {message_id}"
                    )
                return _stored(rec)

            return compare(pick(analysis_a, AnalysisMethod.RULE_BASED), pick(analysis_b, AnalysisMethod.LLM))

    # --- the paid action ----------------------------------------------------------------

    def analyze(self, message_id: int, consent: ClaudeConsent) -> ClaudeAnalyzeResponse:
        s = self._s
        self._raise_if_blocked()

        with Session(self._engine, expire_on_commit=False) as session:
            msg = self._message(session, message_id)
            done = self._existing_response(session, message_id)
            if done is not None:
                return done
            case = CaseRepository(session).get_by_message(message_id)
            case_id = case.id if case else None
            subject, body, fingerprint = msg.subject, msg.body_text, msg.content_fingerprint
            spent = self._spent_24h(session)

        chars = len(subject or "") + len(body)
        if chars > s.claude_max_input_chars:
            raise ClaudeInputTooLargeError(
                f"the message has {chars} characters; the limit for Claude analysis is {s.claude_max_input_chars}"
            )
        _tokens, cost = pricing.worst_case_cost_usd(s.claude_model, chars, s.claude_max_output_tokens)
        if float(cost) > s.claude_max_cost_usd_per_request:
            raise ClaudeCostGuardError(
                f"estimated worst-case cost ${float(cost):.4f} exceeds the per-request limit "
                f"${s.claude_max_cost_usd_per_request:.4f}"
            )
        if spent + float(cost) > s.claude_daily_budget_usd:  # early, friendly refusal; the binding check is _reserve
            raise ClaudeCostGuardError(
                f"the local 24-hour budget ${s.claude_daily_budget_usd:.2f} would be exceeded "
                f"(spent so far ${spent:.4f}, this request up to ${float(cost):.4f})"
            )
        self._check_consent(consent, message_id, fingerprint, float(cost))

        try:
            reservation = self._reserve(message_id, float(cost))
        except _AlreadyAnalyzed:
            with Session(self._engine, expire_on_commit=False) as session:
                done = self._existing_response(session, message_id)
            assert done is not None
            return done

        analyzer = ClaudeAnalyzer(
            self._client_factory(self._key or "", s.claude_timeout_seconds),
            model=s.claude_model,
            max_output_tokens=s.claude_max_output_tokens,
        )
        try:
            result = analyzer.analyze(AnalysisInput(subject=subject, body_text=body))  # no transaction open
        except ClaudeError as exc:
            self._record_failure(reservation, message_id, analyzer, exc)
            raise
        except BaseException:
            self._record_failure(reservation, message_id, analyzer, ClaudeProviderError("unexpected error"))
            raise
        info = analyzer.last_call
        assert info is not None
        return self._persist(reservation, message_id, result, fingerprint, info, case_id)

    def _existing_response(self, session: Session, message_id: int) -> ClaudeAnalyzeResponse | None:
        existing = AnalysisRepository(session).get_for(message_id, ANALYZER_NAME, self._version())
        if existing is None:
            return None
        case = CaseRepository(session).get_by_message(message_id)
        usage = LlmCallRepository(session).for_analysis(existing.id)
        return ClaudeAnalyzeResponse(
            created=False,
            analysis=_stored(existing),
            usage=LlmUsage.model_validate(usage) if usage else None,
            case_id=case.id if case else None,
            note="Already analysed with this model and prompt version: nothing was sent to Anthropic.",
        )

    def _reserve(self, message_id: int, estimated: float) -> int:
        """Atomically reserve the worst-case cost of THIS request before anything is sent.

        One BEGIN IMMEDIATE transaction re-reads the 24-hour spend (reservations included), re-checks the
        budget and inserts a reserved row, so two concurrent requests can never both spend the same remaining
        amount. The same transaction refuses a second identical request for a message while the first one is
        running (it would otherwise be paid twice). It is a local guard, not a billing limit.
        """
        s = self._s
        with Session(self._write_engine, expire_on_commit=False) as session:
            now = self._clock()
            if AnalysisRepository(session).get_for(message_id, ANALYZER_NAME, self._version()) is not None:
                raise _AlreadyAnalyzed()
            calls = LlmCallRepository(session)
            if calls.in_flight(message_id, s.claude_model, now - IN_FLIGHT_WINDOW):
                raise ClaudeInProgressError(
                    f"a Claude request for message {message_id} is already running; wait for it to finish"
                )
            spent = calls.spent_since(now - timedelta(hours=24))
            if spent + estimated > s.claude_daily_budget_usd:
                raise ClaudeCostGuardError(
                    f"the local 24-hour budget ${s.claude_daily_budget_usd:.2f} would be exceeded "
                    f"(reserved or spent so far ${spent:.4f}, this request up to ${estimated:.4f})"
                )
            row = calls.add(LlmCallRecord(
                message_id=message_id, model=s.claude_model, status="reserved",
                estimated_max_cost_usd=estimated, created_at=now,
            ))
            call_id = row.id
            session.commit()
        return call_id

    # --- helpers ------------------------------------------------------------------------

    def _version(self) -> str:
        return f"{PROMPT_VERSION}/{self._s.claude_model}"

    @staticmethod
    def _message(session: Session, message_id: int) -> MessageRecord:
        msg = MessageRepository(session).get(message_id)
        if msg is None:
            raise MessageNotFoundError(f"message {message_id} does not exist")
        if msg.import_status == ImportStatus.CONFLICT.value:
            from ai_ops_hub.errors import ConflictNotAnalyzableError

            raise ConflictNotAnalyzableError(f"message {message_id} was imported as a conflict and is never analysed")
        return msg

    def _check_consent(self, consent: ClaudeConsent, message_id: int, fingerprint: str, estimated_cost: float) -> None:
        """The consent must describe exactly THIS request: message, exact text, model and cost."""
        if not consent.confirm_send_to_anthropic:
            raise ClaudeConsentRequiredError("confirm_send_to_anthropic must be true: the email content is sent to Anthropic")
        if consent.message_id != message_id:
            raise ClaudeConsentRequiredError(
                f"the consent was given for message {consent.message_id}, not message {message_id}; request the preview again"
            )
        if consent.content_fingerprint != fingerprint:
            raise ClaudeConsentRequiredError(
                "the message text changed since the preview (different content fingerprint); request the preview again"
            )
        if consent.model != self._s.claude_model:
            raise ClaudeConsentRequiredError(
                f"the consent names model {consent.model!r} but the configured model is {self._s.claude_model!r}; "
                "request the preview again"
            )
        if consent.accepted_max_cost_usd + 1e-9 < estimated_cost:
            raise ClaudeConsentRequiredError(
                f"accepted_max_cost_usd ${consent.accepted_max_cost_usd:.4f} is below the worst-case estimate "
                f"${estimated_cost:.4f}; request the preview again"
            )

    @staticmethod
    def _apply_usage(record: LlmCallRecord, info: CallInfo | None, status: str, error_type: str | None,
                     analysis_id: int | None = None, billed_in: int | None = None) -> None:
        """Fill the reserved usage row with what actually happened."""
        in_tok = info.input_tokens if info else billed_in
        out_tok = info.output_tokens if info else None
        record.status = status
        record.error_type = error_type
        record.analysis_id = analysis_id
        record.input_tokens = in_tok
        record.output_tokens = out_tok
        record.actual_cost_usd = float(pricing.cost_usd(record.model, in_tok, out_tok or 0)) if in_tok is not None else None
        record.latency_ms = info.latency_ms if info else None
        record.request_id = info.request_id if info else None
        record.values_proposed = info.values_proposed if info else 0
        record.values_rejected = info.values_rejected if info else 0

    def _record_failure(self, reservation: int, message_id: int, analyzer: ClaudeAnalyzer, exc: ClaudeError) -> None:
        """Best effort: a failed (possibly billed) request settles its usage row and leaves an audit event, never an analysis.

        If this write itself fails, the row stays reserved: its worst-case estimate keeps counting."""
        try:
            now = self._clock()
            billed = exc.billed_input_tokens if isinstance(exc, ClaudeBadResponseError) else None
            with Session(self._write_engine, expire_on_commit=False) as session:
                row = LlmCallRepository(session).get(reservation)
                assert row is not None
                self._apply_usage(row, analyzer.last_call, "failed", type(exc).__name__, billed_in=billed)
                session.add(row)
                AuditRepository(session).add(
                    message_id=message_id,
                    event_type=AuditEventType.ANALYSIS_FAILED,
                    actor=Actor.HUMAN,
                    timestamp=now,
                    details={"analysis_method": "llm", "model": analyzer.model, "error_type": type(exc).__name__,
                             "note": "no analysis was stored"},
                )
                session.commit()
        except Exception:  # never mask the original error
            log.exception("could not record a failed Claude request")

    def _persist(self, reservation: int, message_id: int, result, fingerprint: str, info: CallInfo,
                 case_id: int | None) -> ClaudeAnalyzeResponse:
        analyzer_model = self._s.claude_model
        try:
            with Session(self._write_engine, expire_on_commit=False) as session:
                analyses = AnalysisRepository(session)
                msg = MessageRepository(session).get(message_id)
                if msg is None or msg.import_status == ImportStatus.CONFLICT.value:
                    raise MessageNotFoundError(f"message {message_id} is no longer analysable")
                now = self._clock()
                record = analyses.add(message_id=message_id, result=result, source_fingerprint=fingerprint, created_at=now)
                usage = LlmCallRepository(session).get(reservation)
                assert usage is not None
                self._apply_usage(usage, info, "ok", None, analysis_id=record.id)
                session.add(usage)
                session.flush()
                AuditRepository(session).add(
                    message_id=message_id,
                    event_type=AuditEventType.ANALYSIS_COMPLETED,
                    actor=Actor.HUMAN,  # started by an explicit user action, with consent
                    timestamp=now,
                    details={
                        "analysis_id": record.id,
                        "analyzer_name": result.analyzer_name,
                        "analyzer_version": result.analyzer_version,
                        "analysis_method": result.analysis_method.value,
                        "model": analyzer_model,
                        "category": result.category.value,
                        "requires_review": result.requires_review,
                        "field_count": len(result.fields),
                        "values_proposed": info.values_proposed,
                        "values_rejected": info.values_rejected,
                        "input_tokens": info.input_tokens,
                        "output_tokens": info.output_tokens,
                        "source_fingerprint": fingerprint,
                        "note": "optional LLM analysis; no case was created or changed",
                    },
                )
                stored, usage_view = _stored(record), LlmUsage.model_validate(usage)
                session.commit()
        except IntegrityError:  # a concurrent identical request stored it first
            with Session(self._engine, expire_on_commit=False) as session:
                rec = AnalysisRepository(session).get_for(message_id, ANALYZER_NAME, self._version())
                if rec is None:
                    raise
                return ClaudeAnalyzeResponse(created=False, analysis=_stored(rec), usage=None, case_id=case_id,
                                             note="Another identical request stored this analysis first.")
        return ClaudeAnalyzeResponse(
            created=True,
            analysis=stored,
            usage=usage_view,
            case_id=case_id,
            note=("The message's existing case keeps its own analysis; compare the two read-only."
                  if case_id else "No case was created. Create one explicitly from the analysis you choose."),
        )

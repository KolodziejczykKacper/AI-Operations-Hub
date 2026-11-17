"""Domain errors raised by input handling."""


class AIOpsError(Exception):
    """Base class for all application errors."""


class EmptyInputError(AIOpsError):
    """Input contained no usable content."""


class InputTooLargeError(AIOpsError):
    """Input exceeded a configured size limit."""


class MalformedMessageError(AIOpsError):
    """Input could not be interpreted as an email message."""


class MessageNotFoundError(AIOpsError):
    """No stored message has the requested id."""


class ConflictNotAnalyzableError(AIOpsError):
    """Messages imported as conflicts are never analyzed automatically."""


class AnalyzerContractError(AIOpsError):
    """An analyzer returned a result that violates the analyzer contract."""


class CaseNotFoundError(AIOpsError):
    """No case has the requested id."""


class CaseNotAvailableError(AIOpsError):
    """The message has no case (it was never analyzed, or it is a conflicting import)."""


class InvalidTransitionError(AIOpsError):
    """The requested review-status change is not a legal transition."""


class CaseNotEditableError(InvalidTransitionError):
    """Closed (approved / rejected) cases must be reopened before they can be edited."""


class VersionConflictError(AIOpsError):
    """The case changed since the client last read it (optimistic locking)."""

    def __init__(self, message: str, current_version: int) -> None:
        super().__init__(message)
        self.current_version = current_version


class ApprovalBlockedError(AIOpsError):
    """The effective case data has problems that need fixing or an explicit override."""

    def __init__(self, message: str, problems: list[dict]) -> None:
        super().__init__(message)
        self.problems = problems


class InvalidCorrectionError(AIOpsError):
    """A human correction is not a valid value for its field."""

    def __init__(self, message: str, problems: list[dict] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


class ExportError(AIOpsError):
    """Base class for Excel export failures."""


class NoApprovedCasesError(ExportError):
    """No approved case matches the export filter (there is nothing to export)."""


class ExportTooLargeError(ExportError):
    """More cases match than one export may contain; narrow the filter."""

    def __init__(self, message: str, eligible: int, limit: int) -> None:
        super().__init__(message)
        self.eligible = eligible
        self.limit = limit


class InvalidExportFilterError(ExportError):
    """The export filter is malformed (e.g. a date range that ends before it starts)."""


class ExportDatabaseError(ExportError):
    """The database could not be read for the export."""


class ExportGenerationError(ExportError):
    """The workbook could not be generated."""

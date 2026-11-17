"""FastAPI application factory.

``create_app`` takes everything it needs as arguments (settings, engine, analyzer,
clock), so tests build an app on a temporary database without touching global state.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from sqlalchemy import Engine, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ai_ops_hub.analysis import RuleBasedAnalyzer
from ai_ops_hub.analysis.base import Analyzer
from ai_ops_hub.api import routes_cases, routes_claude, routes_exports, routes_messages
from ai_ops_hub.api.deps import Services, ServicesDep
from ai_ops_hub.api.errors import install_error_handlers
from ai_ops_hub.api.limits import BodySizeLimitMiddleware
from ai_ops_hub.api.schemas import Health
from ai_ops_hub.config import Settings, get_settings
from ai_ops_hub.db.engine import create_db_engine, init_db
from ai_ops_hub.services.analysis_service import AnalysisService
from ai_ops_hub.analysis.claude.analyzer import make_client
from ai_ops_hub.services.case_service import CaseService
from ai_ops_hub.services.claude_service import ClaudeClientFactory, ClaudeService
from ai_ops_hub.services.export_service import ExportService
from ai_ops_hub.services.import_service import ImportService, utc_now

# Host headers the local demo answers to. Refusing others blocks DNS-rebinding attacks.
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]

DESCRIPTION = """
Local demo API: import business emails, run the **rule-based** analyzer (pattern matching,
**not AI**), correct the extracted data and explicitly approve or reject cases.

* Nothing is ever approved automatically and nothing is ever sent or executed.
* **No authentication.** Every change is attributed to the fixed label `local_demo_user`;
  that label is not proof of identity. Keep the server on localhost.
"""


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    analyzer: Analyzer | None = None,
    clock: Callable[[], datetime] = utc_now,
    claude_client_factory: ClaudeClientFactory = make_client,
) -> FastAPI:
    settings = settings or get_settings()
    owns_engine = engine is None
    engine = engine or create_db_engine(settings=settings)
    # Creates only MISSING tables; existing tables and data are never altered (no migrations yet).
    init_db(engine)

    services = Services(
        settings=settings,
        engine=engine,
        imports=ImportService(engine, clock),
        analysis=AnalysisService(engine, analyzer or RuleBasedAnalyzer(), clock),
        cases=CaseService(engine, clock),
        exports=ExportService(engine, clock),
        claude=ClaudeService(engine, settings, clock, claude_client_factory),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_engine:
            engine.dispose()

    app = FastAPI(
        title="AI Operations Hub",
        version="0.4.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.services = services

    max_body = max(settings.max_eml_bytes, settings.max_text_chars * 6) + 64 * 1024
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body)  # added last = outermost
    install_error_handlers(app)

    @app.get("/health", response_model=Health, tags=["system"], summary="Health check")
    def health(services: ServicesDep) -> Health:
        with services.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return Health()

    app.include_router(routes_messages.router)
    app.include_router(routes_cases.router)
    app.include_router(routes_exports.router)
    app.include_router(routes_claude.router)
    return app

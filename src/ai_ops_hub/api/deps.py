"""Dependency injection: routes receive a ``Services`` bundle, never build their own."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Path, Request
from sqlalchemy import Engine

from ai_ops_hub.config import Settings
from ai_ops_hub.services.analysis_service import AnalysisService
from ai_ops_hub.services.case_service import CaseService
from ai_ops_hub.services.export_service import ExportService
from ai_ops_hub.services.import_service import ImportService

MAX_DB_ID = 2**63 - 1  # SQLite integer range; larger ids are rejected before reaching the database


@dataclass(frozen=True)
class Services:
    settings: Settings
    engine: Engine
    imports: ImportService
    analysis: AnalysisService
    cases: CaseService
    exports: ExportService


def get_services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(get_services)]
IdPath = Annotated[int, Path(ge=1, le=MAX_DB_ID)]

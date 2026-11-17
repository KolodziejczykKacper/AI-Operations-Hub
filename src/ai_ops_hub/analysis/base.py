"""The analyzer contract.

The application depends only on this Protocol. The deterministic rule-based
analyzer implements it today; a Claude API adapter can implement it later
without touching services, persistence or the schemas.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ai_ops_hub.analysis.schemas import AnalysisInput, AnalysisResult
from ai_ops_hub.schemas import AnalysisMethod


@runtime_checkable
class Analyzer(Protocol):
    name: str  # stable identifier, e.g. "rule_based_analyzer"
    version: str  # bump whenever behaviour changes; stored with every analysis
    method: AnalysisMethod  # how results are produced; never mislabelled

    def analyze(self, message: AnalysisInput) -> AnalysisResult: ...

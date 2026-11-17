"""The structured answer Claude must return, and its strict local validation.

The JSON schema is sent as ``output_config.format`` (schema-constrained output). It carries no
numeric or length constraints because the API does not support them; every constraint is
enforced locally by the pydantic model and by ``verify.py``. The model never gives offsets:
it gives a verbatim ``quote`` and offsets are computed locally.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_ops_hub.analysis.schemas import Category, FieldName

CATEGORIES = [c.value for c in Category]
FIELD_NAMES = [n.value for n in FieldName]
MAX_FIELDS = 30
MAX_QUOTE_CHARS = 300
MAX_NORMALIZED_CHARS = 300
MAX_NOTE_CHARS = 300

RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["category", "category_alternatives", "fields"],
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "category_alternatives": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "quote", "source", "normalized", "status", "note"],
                "properties": {
                    "name": {"type": "string", "enum": FIELD_NAMES},
                    "quote": {"type": "string"},
                    "source": {"type": "string", "enum": ["subject", "body"]},
                    "normalized": {"type": "string"},
                    "status": {"type": "string", "enum": ["valid", "ambiguous", "invalid"]},
                    "note": {"type": "string"},
                },
            },
        },
    },
}


class RawField(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: FieldName
    quote: str = Field(max_length=MAX_QUOTE_CHARS)
    source: Literal["subject", "body"]
    normalized: str = Field(max_length=MAX_NORMALIZED_CHARS)
    status: Literal["valid", "ambiguous", "invalid"]
    note: str = Field(max_length=MAX_NOTE_CHARS)


class RawAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Category
    category_alternatives: list[Category] = Field(max_length=4)
    fields: list[RawField] = Field(max_length=MAX_FIELDS)

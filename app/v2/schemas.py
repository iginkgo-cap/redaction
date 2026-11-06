"""v2 请求模型（ADR-002）。extra=forbid：未知字段（如 markdown_aware）直接 400。"""
from __future__ import annotations

import base64
import binascii
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..schemas import CANONICAL_KINDS, _normalize_kinds

INCLUDE_KINDS = CANONICAL_KINDS | {"CUSTOM"}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class FileBody(_Model):
    """file 项（ADR-002 §3.2）：目前只支持 PDF，只有 /v2/redact 接受。"""

    name: str = ""
    content_type: Literal["application/pdf"]
    data: str  # base64

    @field_validator("data")
    @classmethod
    def _base64(cls, v: str) -> str:
        try:
            base64.b64decode(v, validate=True)
        except binascii.Error as exc:
            raise ValueError("data must be base64") from exc
        return v

    def content(self) -> bytes:
        return base64.b64decode(self.data)


class Item(_Model):
    id: str
    text: str | None = None
    json_: Any = Field(default=None, alias="json")
    file: FileBody | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "Item":
        if len({"text", "json_", "file"} & self.model_fields_set) != 1:
            raise ValueError("item must have exactly one of text / json / file")
        if "text" in self.model_fields_set and self.text is None:
            raise ValueError("text must be a string")
        if "file" in self.model_fields_set and self.file is None:
            raise ValueError("file must be an object")
        return self

    def as_dict(self) -> dict[str, Any]:
        if "file" in self.model_fields_set:
            return {"file": self.file}
        return {"text": self.text} if "text" in self.model_fields_set else {"json": self.json_}


class _Request(_Model):
    namespace: str = Field(min_length=1)
    source_app: str | None = None


class DetectExclude(_Model):
    kinds: list[str] = []

    _norm_kinds = field_validator("kinds")(_normalize_kinds)


class ExcludeValue(_Model):
    value: str = Field(min_length=1)
    reason: Literal["false_positive"] | None = None


class RedactExclude(DetectExclude):
    values: list[ExcludeValue] = []


class IncludeValue(_Model):
    value: str = Field(min_length=1)
    kind: str

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in INCLUDE_KINDS:
            raise ValueError(f"unknown kind: {v!r}")
        return v


class Include(_Model):
    values: list[IncludeValue] = []


class DetectRequest(_Request):
    items: list[Item]
    level: Literal["full", "regex", "none"] = "full"
    allow_degrade: bool = True
    exclude: DetectExclude = DetectExclude()


class RedactRequest(DetectRequest):
    strategy: Literal["placeholder"] = "placeholder"  # fixed_text / delete 随 LOOK-6876
    known_placeholders: list[str] = []
    exclude: RedactExclude = RedactExclude()
    include: Include = Include()


class LookupRequest(_Request):
    placeholders: list[str]


class RestoreItem(_Model):
    id: str
    text: str


class RestoreRequest(_Request):
    items: list[RestoreItem]
    on_unresolved: Literal["keep", "redact_kind"] = "keep"


class RemoveRequest(_Request):
    confirm: str

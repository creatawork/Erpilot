"""Validated document provenance and effective periods; no business dependencies."""

from datetime import datetime
from hashlib import sha256
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

NonEmpty = Annotated[str, Field(strict=True, min_length=1)]


def policy_digest(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return sha256(normalized.encode("utf-8")).hexdigest()


class PolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    titles: list[NonEmpty] = Field(min_length=1)
    values: list[NonEmpty] = Field(min_length=1)

    @field_validator("titles", "values")
    @classmethod
    def unique_entries(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("规则小节和值不能重复")
        return values


class PolicyMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: str = Field(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.md$")
    policy_id: NonEmpty
    version: NonEmpty
    effective_from: datetime
    effective_until: datetime | None = None
    source_sha256: str = Field(strict=True, pattern=r"^[0-9a-f]{64}$")
    rules: dict[NonEmpty, PolicyRule] = Field(default_factory=dict)

    @field_validator("effective_from", "effective_until", mode="before")
    @classmethod
    def no_epoch_coercion(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("政策时间须为带时区的 ISO 时间")
        return value

    @field_validator("effective_from", "effective_until")
    @classmethod
    def aware_time(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("政策时间必须带时区")
        return value

    @model_validator(mode="after")
    def positive_interval(self):
        if self.effective_until is not None and self.effective_until <= self.effective_from:
            raise ValueError("结束时间必须晚于生效时间")
        return self

    def active_at(self, at: datetime) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("查询时间必须带时区")
        return self.effective_from <= at and (
            self.effective_until is None or at < self.effective_until
        )

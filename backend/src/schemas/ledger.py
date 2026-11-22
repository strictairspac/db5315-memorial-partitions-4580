from typing import Any

from pydantic import BaseModel, Field


class IngestForm(BaseModel):
    records: list[dict[str, Any]] = Field(min_length=1, max_length=2000)


class MonthForm(BaseModel):
    month: str = Field(min_length=10, max_length=10)


class MigrationForm(BaseModel):
    month: str = Field(min_length=10, max_length=10)

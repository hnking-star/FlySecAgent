"""Pydantic 入参/出参模型。

本任务（Task 4）只放 /hook/* 相关。/observer/* 的 Schema 在 Task 5。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, StringConstraints
from typing_extensions import Annotated

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=200)]
TargetStr = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
ObjectiveStr = Annotated[str, StringConstraints(min_length=1, max_length=2000)]


class ProjectHint(BaseModel):
    target: TargetStr | None = None
    objective: ObjectiveStr | None = None


class ProjectEnsureInput(BaseModel):
    session_id: NonEmptyStr
    hint: ProjectHint | None = None


class ProjectEnsureOutput(BaseModel):
    ok: bool = True
    created: bool


class RecordIngestInput(BaseModel):
    session_id: NonEmptyStr
    tool_name: NonEmptyStr
    call_key: NonEmptyStr | None = None
    tool_input: Any = None
    tool_response: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecordIngestOutput(BaseModel):
    ok: bool = True
    record_id: int


class MapPendingOutput(BaseModel):
    ok: bool = True
    revision: str | None = None
    map_text: str | None = None


class MapAckInput(BaseModel):
    session_id: NonEmptyStr
    revision: NonEmptyStr


class MapAckOutput(BaseModel):
    ok: bool = True
    acked: bool = True

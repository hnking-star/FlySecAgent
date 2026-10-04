"""Single source of truth: host transport DTOs and FlySec evidence-memory v2."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

# ---------------------------------------------------------------------------
# 通用约束
# ---------------------------------------------------------------------------

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=200)]
TargetStr = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
ObjectiveStr = Annotated[str, StringConstraints(min_length=1, max_length=2000)]

IdStr = Annotated[
    str, StringConstraints(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,79}$")
]
EndpointStr = Annotated[
    str,
    StringConstraints(
        pattern=r"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS|UNKNOWN) \S.*$",
        max_length=400,
    ),
]
Action1200 = Annotated[str, StringConstraints(min_length=1, max_length=1200)]
Result4000 = Annotated[str, StringConstraints(min_length=1, max_length=4000)]
Subject120 = Annotated[str, StringConstraints(min_length=1, max_length=120)]
Purpose400 = Annotated[str, StringConstraints(min_length=1, max_length=400)]
ParamName = Annotated[str, StringConstraints(min_length=1, max_length=80)]


# ---------------------------------------------------------------------------
# /hook/* （Task 4）
# ---------------------------------------------------------------------------


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


# Memory protocol v2. Transport identity is never a model argument.

class MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Topic(MemoryModel):
    id: IdStr
    title: Subject120
    summary: str = Field("", max_length=600)
    api_ids: list[IdStr] = Field(default_factory=list, max_length=512)
    origin_fact_ids: list[IdStr] = Field(default_factory=list, max_length=16)


class Fact(MemoryModel):
    id: IdStr
    topic_id: IdStr
    statement: str = Field(min_length=1, max_length=1200)
    kind: Literal["observation", "hypothesis"] = "observation"
    scope: str | None = Field(None, max_length=600)
    evidence_ids: list[int] = Field(min_length=1, max_length=16)
    supersedes: IdStr | None = None


class TestRecord(MemoryModel):
    id: IdStr
    topic_id: IdStr
    api_ids: list[IdStr] = Field(default_factory=list, max_length=32)
    action: Action1200
    result: Result4000
    execution: Literal["completed", "error", "denied", "interrupted", "unknown"]
    outcome: Literal["supports", "contradicts", "inconclusive", "not_evaluated"]
    scope: str | None = Field(None, max_length=600)
    evidence_ids: list[int] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def consistent_outcome(self):
        if self.execution != "completed" and self.outcome != "not_evaluated":
            raise ValueError("an incomplete/failed execution cannot evaluate the target hypothesis")
        return self


class ApiParam(MemoryModel):
    name: ParamName
    description: str | None = Field(None, max_length=400)


class ApiEntry(MemoryModel):
    id: IdStr
    endpoint: EndpointStr
    purpose: Purpose400
    parameters: list[ApiParam] = Field(default_factory=list, max_length=32)
    evidence_ids: list[int] = Field(min_length=1, max_length=16)


class OpenQuestion(MemoryModel):
    id: IdStr
    topic_id: IdStr
    question: str = Field(min_length=1, max_length=600)
    api_ids: list[IdStr] = Field(default_factory=list, max_length=32)
    evidence_ids: list[int] = Field(min_length=1, max_length=16)
    status: Literal["open", "resolved"] = "open"
    resolution: str | None = Field(None, max_length=600)

    @model_validator(mode="after")
    def resolution_required(self):
        if self.status == "resolved" and not self.resolution:
            raise ValueError("resolved questions need a concrete resolution and evidence")
        return self


class MemoryReadInput(MemoryModel):
    mode: Literal["summary", "records", "record", "state"] = "summary"
    record_id: int | None = Field(None, ge=1)
    offset: int = Field(0, ge=0)
    length: int = Field(8192, ge=1, le=65536)
    after_id: int = Field(0, ge=0)
    limit: int = Field(50, ge=1, le=100)
    topic_ids: list[IdStr] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def record_required(self):
        if self.mode == "record" and self.record_id is None:
            raise ValueError("record_id is required for record mode")
        return self


class MemoryCommitInput(MemoryModel):
    protocol: Literal[2] = 2
    revision: str | None
    unchanged_reason: str | None = Field(None, min_length=1, max_length=400)
    # Flat append-only records must not inherit v1's small *topic* limits:
    # v1 could hold many nested attempts per topic. Bound request size without
    # forcing ordinary large-JS windows to omit their APIs or test records.
    topics: list[Topic] = Field(default_factory=list, max_length=256)
    facts: list[Fact] = Field(default_factory=list, max_length=1024)
    tests: list[TestRecord] = Field(default_factory=list, max_length=1024)
    apis: list[ApiEntry] = Field(default_factory=list, max_length=512)
    questions: list[OpenQuestion] = Field(default_factory=list, max_length=256)

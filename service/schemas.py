"""Pydantic 入参/出参模型。

- Task 4：/hook/*
- Task 5：/observer/* 及其子结构（Attempt / Assessment / ApiEntry / Guidance）
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, StringConstraints, model_validator
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
EvidenceRef = Annotated[str, StringConstraints(pattern=r"^record:[1-9][0-9]*$")]
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
Conclusion600 = Annotated[str, StringConstraints(min_length=1, max_length=600)]
Basis1200 = Annotated[str, StringConstraints(min_length=1, max_length=1200)]
Purpose400 = Annotated[str, StringConstraints(min_length=1, max_length=400)]
Line400 = Annotated[str, StringConstraints(min_length=1, max_length=400)]
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


# ---------------------------------------------------------------------------
# /observer/context （Task 5）
# ---------------------------------------------------------------------------


class ContextInput(BaseModel):
    mode: Literal[
        "summary", "window_records", "record_detail", "blackboard", "history_record"
    ] = "summary"
    record_id: int | None = None
    offset: int = Field(0, ge=0)
    length: int = Field(8192, ge=1, le=65536)
    assessment_ids: list[IdStr] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _check_record_id(self):
        if self.mode in ("record_detail", "history_record") and self.record_id is None:
            raise ValueError("record_id required for this mode")
        return self


# ---------------------------------------------------------------------------
# /observer/submit （Task 5）
# ---------------------------------------------------------------------------


class Attempt(BaseModel):
    id: IdStr
    action: Action1200
    result: Result4000
    assessment: str | None = Field(None, max_length=1200)
    evidenceRefs: list[EvidenceRef] = Field(min_length=1, max_length=16)


class Assessment(BaseModel):
    id: IdStr
    subject: Subject120
    status: Literal["inferred-open", "tried-hit", "tried-miss", "scan-class"]
    role: Literal["direction", "endpoint", "path"] = "direction"
    api: str | None = Field(None, max_length=120)
    conclusion: Conclusion600
    basis: Basis1200
    uncertainty: str | None = Field(..., max_length=1200)
    evidenceRefs: list[EvidenceRef] = Field(min_length=1, max_length=16)
    attempts: list[Attempt] = Field(default_factory=list, max_length=20)
    dependsOn: list[IdStr] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _check_attempts(self):
        if self.status != "inferred-open" and not self.attempts:
            raise ValueError(f"status={self.status} requires at least one attempt")
        return self


class ApiTest(BaseModel):
    id: IdStr
    action: Action1200
    result: Result4000
    record_ids: list[int] = Field(min_length=1, max_length=16)


class ApiParam(BaseModel):
    name: ParamName
    description: str | None = Field(None, max_length=400)


class ApiEntry(BaseModel):
    id: IdStr
    endpoint: EndpointStr
    purpose: Purpose400
    parameters: list[ApiParam] = Field(default_factory=list, max_length=32)
    tests: list[ApiTest] = Field(default_factory=list, max_length=32)


class Guidance(BaseModel):
    hypothesis: str | None = Field(None, max_length=400)
    lock: str | None = Field(None, max_length=400)
    angleIds: list[IdStr] = Field(default_factory=list, max_length=4)
    confirmedIds: list[IdStr] = Field(default_factory=list, max_length=8)
    tension: list[Line400] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def _check_tension(self):
        if len(self.tension) not in (0, 2):
            raise ValueError("tension must have 0 or 2 items")
        return self


class SubmitInput(BaseModel):
    baseRevision: str | None
    upserts: list[Assessment] = Field(default_factory=list, max_length=30)
    retireIds: list[IdStr] = Field(default_factory=list, max_length=32)
    apis: list[ApiEntry] = Field(default_factory=list, max_length=32)
    guidance: Guidance | None = None

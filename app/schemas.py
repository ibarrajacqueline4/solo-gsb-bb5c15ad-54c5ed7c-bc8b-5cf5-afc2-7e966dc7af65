from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

COMMITMENT_HASH_PATTERN = r"^[0-9a-fA-F]{64}$"


def require_timezone_aware(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("截止时间必须携带时区偏移（建议使用 UTC，如 2030-01-01T00:00:00Z）")
    return v


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    commit_deadline: datetime = Field(description="提交截止时间（须含时区，建议 UTC）")
    reveal_deadline: datetime = Field(description="揭示截止时间（须含时区，建议 UTC）")

    @field_validator("commit_deadline", "reveal_deadline")
    @classmethod
    def must_be_timezone_aware(cls, v: datetime) -> datetime:
        return require_timezone_aware(v)


class CommitmentCreate(BaseModel):
    commitment_hash: str = Field(
        pattern=COMMITMENT_HASH_PATTERN,
        description="SHA-256 承诺值，64 位十六进制",
        examples=["9f2c..."],
    )


class CommitmentBatchCreate(BaseModel):
    # 元素不在这里做 pattern 校验：格式非法是逐项冲突（409）而非请求体 422
    commitment_hashes: list[str] = Field(
        min_length=1,
        max_length=100,
        description="1～100 个 SHA-256 承诺值（64 位十六进制），按输入顺序在同一事务中原子提交",
    )


class RevealCreate(BaseModel):
    original_text: str = Field(description="预测原文（UTF-8）")
    salt: str = Field(min_length=1, max_length=256, description="随机盐（UTF-8）")


class RevealBatchItem(BaseModel):
    # 不在此处施加盐的长度约束：盐为空/超长是逐项冲突（INVALID_FORMAT，409），
    # 而非整个请求体的 422；字段类型错误或字段缺失才是请求体 422
    original_text: str = Field(description="预测原文（UTF-8）")
    salt: str = Field(description="随机盐（UTF-8，1～256 字符）")


class RevealBatchCreate(BaseModel):
    reveals: list[RevealBatchItem] = Field(
        min_length=1,
        max_length=100,
        description="1～100 组原文与盐，按输入顺序在同一事务中原子揭示",
    )


class DeadlineExtensionCreate(BaseModel):
    commit_deadline: datetime = Field(description="新的提交截止时间（须含时区，建议 UTC）")
    reveal_deadline: datetime = Field(description="新的揭示截止时间（须含时区，建议 UTC）")
    reason: str = Field(min_length=1, max_length=500, description="延长原因（记录进审计与时间线）")

    @field_validator("commit_deadline", "reveal_deadline")
    @classmethod
    def must_be_timezone_aware(cls, v: datetime) -> datetime:
        return require_timezone_aware(v)


class DeadlineExtensionOut(BaseModel):
    event_id: UUID
    old_commit_deadline: datetime
    old_reveal_deadline: datetime
    new_commit_deadline: datetime
    new_reveal_deadline: datetime
    reason: str
    extended_at: datetime


class EventOut(BaseModel):
    id: UUID
    title: str
    description: Optional[str]
    commit_deadline: datetime
    reveal_deadline: datetime
    created_at: datetime
    status: str  # commit | reveal | closed
    commitment_count: int
    reveal_count: int


class CommitmentOut(BaseModel):
    id: UUID
    event_id: UUID
    commitment_hash: str
    created_at: datetime
    revealed: bool


class CommitmentBatchOut(BaseModel):
    count: int = Field(description="本次创建的承诺数量（等于输入哈希数量）")
    commitments: list[CommitmentOut] = Field(description="按输入顺序创建的承诺记录")


class RevealOut(BaseModel):
    id: UUID
    event_id: UUID
    commitment_id: UUID
    commitment_hash: str
    recomputed_hash: str
    hash_scheme: str
    revealed_at: datetime
    verified: bool


class RevealBatchOut(BaseModel):
    count: int = Field(description="本次写入的揭示数量（等于输入组数）")
    reveals: list[RevealOut] = Field(description="按输入顺序写入的揭示记录，全部 verified=true")


class TimelineEntry(BaseModel):
    seq: int
    type: str  # event_created | commitment_submitted | reveal_accepted | deadline_extended
    at: datetime
    detail: dict[str, Any]


class TimelineOut(BaseModel):
    event_id: UUID
    status: str
    generated_at: datetime
    hash_scheme: str
    entries: list[TimelineEntry]


class ConflictRequest(BaseModel):
    event_ids: list[UUID] = Field(
        default_factory=list,
        description="待分析的事件 ID 列表（重复 ID 自动去重，空列表返回空结果）",
    )


class ConflictSegmentOut(BaseModel):
    start: datetime = Field(description="片段起点（含，UTC）")
    end: datetime = Field(description="片段终点（不含，UTC，半开边界）")
    phase: str  # commit | reveal
    event_ids: list[UUID] = Field(description="该时段同时处于同一阶段的事件 ID（已排序）")


class ConflictsOut(BaseModel):
    generated_at: datetime
    phases: list[str]
    count: int = Field(description="冲突片段数量")
    segments: list[ConflictSegmentOut]


class ScheduleRequest(BaseModel):
    event_ids: list[UUID] = Field(
        default_factory=list,
        description="待排程的事件 ID 列表（重复 ID 自动去重，空列表返回空结果）",
    )
    # 不用 int 类型约束：非法上限（缺失、null、非整数、布尔、小于 1）须走统一
    # 错误结构（api_error 的 400 INVALID_TRACK_LIMIT），而不是 pydantic 的 422
    track_limit: Any = Field(default=None, description="可用 track 数（>= 1 的整数）")


class ScheduleAssignmentOut(BaseModel):
    event_id: UUID
    commit_track: Optional[int] = Field(
        description="commit 阶段分配的轨道（1 起编号）；零长度窗口为 null"
    )
    reveal_track: Optional[int] = Field(
        description="reveal 阶段分配的轨道（1 起编号，与 commit 轨道相互独立）；零长度窗口为 null"
    )


class ScheduleConflictPairOut(BaseModel):
    phase: str  # commit | reveal
    event_id_1: UUID = Field(description="冲突对中按 UUID 升序的第一个事件")
    event_id_2: UUID = Field(description="冲突对中按 UUID 升序的第二个事件")


class ScheduleOut(BaseModel):
    track_limit: int = Field(description="请求的可用 track 数")
    required_tracks: int = Field(
        description="最小所需 track 数（commit / reveal 两阶段各自最大并发深度的较大值）"
    )
    feasible: bool = Field(description="最小所需轨道数是否不超过 track_limit")
    assignments: list[ScheduleAssignmentOut] = Field(
        description="按 event_id 升序的逐事件轨道分配"
    )
    conflicts: list[ScheduleConflictPairOut] = Field(
        description="feasible=false 时导致超出上限的同阶段冲突事件对（去重排序）；feasible=true 时为空"
    )


class HashOut(BaseModel):
    commitment_hash: str
    hash_scheme: str

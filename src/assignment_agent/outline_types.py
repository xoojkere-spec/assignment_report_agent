"""证据驱动的大纲示例修订契约；不允许返回标题、预算或评分项补丁。"""

from typing import Annotated

from pydantic import Field, field_validator

from assignment_agent.schemas import Record

OUTLINE_REVISION_VERSION = 3


class OutlineExamplePlan(Record):
    purpose: str = Field(min_length=1, max_length=5000)
    evidence_needs: list[str] = Field(min_length=1, max_length=12)

    @field_validator("purpose")
    @classmethod
    def nonblank_purpose(cls, value):
        if not value.strip():
            raise ValueError("修订后的章节目标不能为空")
        return value.strip()

    @field_validator("evidence_needs")
    @classmethod
    def nonblank_needs(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("修订后的证据需求不能为空")
        return [value.strip() for value in values]


class OutlineRevisionProposal(Record):
    replacement: OutlineExamplePlan | None = None
    reason: str = Field(min_length=1, max_length=1500)
    source_ids: list[Annotated[str, Field(pattern=r"^src-[0-9a-f]{24}$")]] = Field(
        default_factory=list,
        max_length=10,
        validate_default=True,
        description="replacement 非 null 时必须填写至少一个证据池完整来源 ID；不修订时可为空。",
    )

    @field_validator("source_ids")
    @classmethod
    def require_revision_sources(cls, values, info):
        if info.data.get("replacement") is not None and not values:
            raise ValueError("示例修订必须绑定至少一个完整来源 ID")
        return values


class OutlineRevisionReview(Record):
    hard_requirements_preserved: bool
    scope_preserved: bool
    evidence_supported: bool
    issues: list[str] = Field(
        default_factory=list, max_length=12, description="阻断大纲修订的具体问题；通过时为空。"
    )
    notes: list[str] = Field(
        default_factory=list, max_length=12, description="后续写作的限定、来源层级与呈现提醒。"
    )

    @property
    def accepted(self):
        return (
            self.hard_requirements_preserved
            and self.scope_preserved
            and self.evidence_supported
            and not self.issues
        )

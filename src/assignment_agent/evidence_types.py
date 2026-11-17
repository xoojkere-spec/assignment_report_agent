"""正文证据契约：引用和图片必须绑定来源库记录，不能只提供网址。"""

from typing import Literal

from pydantic import Field

from assignment_agent.schemas import Record

EVIDENCE_POLICY_VERSION = 2


class CitationUse(Record):
    source_id: str = Field(pattern=r"^src-[0-9a-f]{24}$")
    claim: str = Field(min_length=5, max_length=1500)
    quote: str = Field(min_length=20, max_length=1500)
    page: int | None = Field(default=None, ge=1)


class FigureUse(Record):
    figure_id: str = Field(pattern=r"^fig-[0-9a-f]{24}$")
    caption: str = Field(min_length=3, max_length=600)
    explanation: str = Field(min_length=5, max_length=1500)


class EvidenceDraft(Record):
    content: str = Field(min_length=1)
    citations: list[CitationUse] = Field(default_factory=list, max_length=30)
    figures: list[FigureUse] = Field(default_factory=list, max_length=12)


class CitationRepair(Record):
    index: int = Field(ge=0)
    quote: str = Field(min_length=20, max_length=1500)
    page: int | None = Field(ge=1)


class CitationRepairs(Record):
    repairs: list[CitationRepair] = Field(default_factory=list, max_length=30)


class CitationPassageSelection(Record):
    index: int = Field(ge=0)
    passage_index: int = Field(ge=0)


class CitationPassageSelections(Record):
    repairs: list[CitationPassageSelection] = Field(default_factory=list, max_length=30)


class CitationVerdict(Record):
    index: int = Field(ge=0)
    supported: bool
    reason: str = Field(min_length=1, max_length=1000)


class FigureVerdict(Record):
    figure_id: str = Field(pattern=r"^fig-[0-9a-f]{24}$")
    relevant: bool
    caption_accurate: bool
    license_applicable: bool = Field(
        description="单图许可适用性的信息记录；未知或不适用填 false，仅作提示，不阻断写作或导出。"
    )
    reason: str = Field(min_length=1, max_length=1000)


class EvidenceGap(Record):
    kind: Literal["source", "figure"]
    detail: str = Field(min_length=1, max_length=1000)


class EvidenceVerdict(Record):
    citations: list[CitationVerdict]
    figures: list[FigureVerdict]
    issues: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="仅填写阻止本章通过的真实错误；通过时为空。建议及核验局限放 notes。",
    )
    notes: list[str] = Field(default_factory=list, max_length=10)
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list, max_length=6)

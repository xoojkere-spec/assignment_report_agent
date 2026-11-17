"""来源库的数据契约；元数据核对不等同于正文论据或转载许可核验。"""

import re
from typing import Literal

from pydantic import Field, field_validator

from assignment_agent.evidence_types import EvidenceGap
from assignment_agent.schemas import Record


class WebCandidate(Record):
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(min_length=1, max_length=4096)


class ResearchPlan(Record):
    queries: list[str] = Field(min_length=1, max_length=6)
    web_queries: list[str] = Field(default_factory=list, max_length=3)
    figure_queries: list[str] = Field(default_factory=list, max_length=3)
    source_candidates: list[WebCandidate] = Field(default_factory=list, max_length=5)

    @field_validator("queries")
    @classmethod
    def clean_queries(cls, values):
        values = list(dict.fromkeys(value.strip() for value in values))
        if any(not value or len(value) > 200 for value in values):
            raise ValueError("每条检索词需为 1 至 200 个字符")
        return values

    @field_validator("web_queries", "figure_queries")
    @classmethod
    def clean_web_queries(cls, values):
        return cls.clean_queries(values) if values else []


class ResearchSelection(Record):
    source_ids: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, values):
        return list(dict.fromkeys(values))


class ResearchRequest(Record):
    queries: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("queries")
    @classmethod
    def clean_queries(cls, values):
        return ResearchPlan(queries=values).queries if values else []


class LibraryReview(Record):
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["source", "figure"]
    entry_id: str = Field(pattern=r"^(?:src|fig)-[0-9a-f]{24}$")
    action: Literal["accept", "reject"]
    note: str = Field(default="", max_length=2000)
    permission_confirmed: bool = False


class Paper(Record):
    title: str = Field(min_length=1, max_length=1000)
    authors: list[str] = Field(default_factory=list)
    family_names: list[str | None] = Field(default_factory=list)
    year: int | None = None
    doi: str | None = None
    url: str
    pdf_urls: list[str] = Field(default_factory=list)
    provider: str
    abstract: str = ""
    landing_urls: list[str] = Field(default_factory=list)
    is_open_access: bool = False
    metadata_pending: bool = False
    # 索引许可仅用于发现候选，图片使用仍须核对 PDF 原文和单图版权。
    license_hints: list[dict[str, str | None]] = Field(default_factory=list, max_length=10)

    @field_validator("authors")
    @classmethod
    def clean_authors(cls, values):
        """学术索引可能将空姓氏记为尾随连字符，不能把它当成正文作者名。"""
        return [
            name for value in values if (name := re.sub(r"[\s–—-]+$", "", " ".join(value.split())))
        ]


class ResearchError(ValueError):
    """只包含可展示的固定错误，不回显响应正文和网络异常中的敏感内容。"""


class EvidenceFailure(ResearchError):
    def __init__(self, issues):
        self.issues = list(dict.fromkeys(issues))[:20]
        super().__init__("；".join(self.issues))


class MissingEvidence(EvidenceFailure):
    def __init__(self, issues, gaps: list[EvidenceGap]):
        self.gaps = gaps
        super().__init__(issues)

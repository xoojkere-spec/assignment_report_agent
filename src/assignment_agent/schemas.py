"""定义可序列化的任务、草稿与确认记录。"""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequirementDetails(Record):
    """核心要求缺失时，仍保留已提取的引用、插图和其他约束。"""

    language: str | None = None
    task_type: str | None = None
    citation_style: str | None = None
    word_count_rules: str | None = None
    page_limit: int | None = Field(default=None, gt=0)
    page_count_rules: str | None = None
    requirements: list[str] = Field(default_factory=list)
    min_sources: int = Field(
        default=3,
        ge=3,
        strict=True,
        description="正文实际使用的独立引用来源最低数量；同一来源重复引用只算一个",
    )
    min_web_images: int = Field(
        default=2,
        ge=0,
        strict=True,
        description="网上检索且实际插入正文的不同图片最低数量；原创图、重复图和链接不计入",
    )


class AssignmentSpec(RequirementDetails):
    title: str = Field(min_length=1)
    sections: list[str] = Field(default_factory=list)
    section_mode: Literal["manual", "recommend"] = "manual"
    word_limit: int = Field(gt=0)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("题目不能为空")
        return value.strip()

    @field_validator("sections")
    @classmethod
    def clean_sections(cls, values: list[str]) -> list[str]:
        values = [value.strip() for value in values]
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError("部分名称不能为空或重复")
        return values

    @model_validator(mode="after")
    def require_manual_sections(self) -> "AssignmentSpec":
        if self.section_mode == "manual" and not self.sections:
            raise ValueError("手动划分时至少填写一个真实章节名；也可选择由 AI 推荐")
        return self


class SectionPlan(Record):
    title: str = Field(min_length=1)
    word_budget: int = Field(gt=0)
    purpose: str = Field(min_length=1)
    rubric_coverage: list[str] = Field(default_factory=list)
    evidence_needs: list[str] = Field(default_factory=list)

    @field_validator("title", "purpose")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("章节名称和目标不能为空")
        return value.strip()


class OutlinePlan(Record):
    parts: list[SectionPlan] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_titles(self) -> "OutlinePlan":
        names = [part.title for part in self.parts]
        if len(names) != len(set(names)):
            raise ValueError("推荐章节名称不能重复")
        return self

    def to_markdown(self, title: str) -> str:
        lines = [f"# {title}：推荐章节划分", "", "确认此规划后，将从第一章开始起草正文。"]
        for index, part in enumerate(self.parts, 1):
            lines += [
                "",
                f"## {index}. {part.title}",
                f"字数预算：{part.word_budget}",
                f"写作目标：{part.purpose}",
                "对应要求：" + "；".join(part.rubric_coverage),
                "证据需求：" + "；".join(part.evidence_needs),
            ]
        return "\n".join(lines)


class Artifact(Record):
    artifact_id: str
    version: int = Field(ge=1)
    content: str = Field(min_length=1)


class Decision(Record):
    interrupt_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    action: Literal["approve", "revise"]
    feedback: str = ""

    @model_validator(mode="after")
    def require_feedback(self) -> "Decision":
        if self.action == "revise" and not self.feedback.strip():
            raise ValueError("要求修改时必须提供意见")
        return self


class Approval(Decision):
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SourceUnit(Record):
    location: str
    text: str
    method: Literal["text", "ocr"] = "text"
    confidence: float | None = Field(default=None, ge=0, le=1)


class Material(Record):
    source_id: str
    path: str
    role: Literal["assignment", "rubric", "course"]
    sha256: str
    units: list[SourceUnit]


class RequirementFact(Record):
    field: str
    value: str
    source_id: str
    location: str
    quote: str


class Analysis(Record):
    facts: list[RequirementFact] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    spec: AssignmentSpec | None = None
    requirement_details: RequirementDetails = Field(default_factory=RequirementDetails)

    @model_validator(mode="after")
    def retain_details(self) -> "Analysis":
        if self.spec is not None:
            self.requirement_details = RequirementDetails.model_validate(
                {name: getattr(self.spec, name) for name in RequirementDetails.model_fields}
            )
        else:
            # 兼容早期仅返回 facts 的解析结果；有冲突的单值不替用户决定。
            details = self.requirement_details.model_dump()
            for name in RequirementDetails.model_fields:
                if name in self.requirement_details.model_fields_set:
                    continue
                values = list(
                    dict.fromkeys(fact.value for fact in self.facts if fact.field == name)
                )
                if name == "requirements" and values:
                    details[name] = values
                elif len(values) == 1:
                    if name in {"min_sources", "min_web_images", "page_limit"}:
                        if values[0].isascii() and values[0].isdigit():
                            details[name] = (
                                int(values[0])
                                if name == "page_limit"
                                else max(details[name], int(values[0]))
                            )
                    else:
                        details[name] = values[0]
            self.requirement_details = RequirementDetails.model_validate(details)
        return self


class SkillDocument(Record):
    path: str
    sha256: str
    content: str


class Clarification(Record):
    interrupt_id: str = Field(min_length=1)
    spec: AssignmentSpec
    note: str = Field(min_length=1)
    assessment: dict[str, str] = Field(default_factory=dict)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("note")
    @classmethod
    def require_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请说明澄清或冲突处理决定")
        return value


class RequirementReply(Record):
    interrupt_id: str = Field(min_length=1)
    answer: str = Field(min_length=1, max_length=4000)

    @field_validator("answer")
    @classmethod
    def clean_answer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请回复具体要求，或说明没有额外要求")
        return value.strip()


class RequirementAssessment(Record):
    spec: AssignmentSpec
    reasons: dict[str, str] = Field(min_length=1)


class ModelProfile(Record):
    kind: Literal["fake", "openai_compatible"] = "fake"
    base_url: str | None = None
    model: str = "fake"


class CompletionRequest(Record):
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdvisorQuestion(Record):
    interrupt_id: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=4000)

    @field_validator("question")
    @classmethod
    def clean_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请输入想了解的问题")
        return value.strip()


class RunState(Record):
    @model_validator(mode="before")
    @classmethod
    def upgrade_known_state(cls, value):
        from assignment_agent.state_versions import upgrade_state

        return upgrade_state(value)

    schema_version: Literal[1] = 1
    model_profile: ModelProfile = Field(default_factory=ModelProfile)
    spec: AssignmentSpec | None = None
    requirements_confirmed: bool = False
    parent_run_id: str | None = None
    outline_plan: OutlinePlan | None = None
    materials: list[Material] = Field(default_factory=list)
    analysis: Analysis | None = None
    clarifications: list[Clarification] = Field(default_factory=list)
    skill_root: str | None = None
    skill_manifest: dict[str, str] = Field(default_factory=dict)
    skill_documents: dict[str, SkillDocument] = Field(default_factory=dict)
    active_skill_paths: list[str] = Field(default_factory=list)
    status: Literal["running", "awaiting_user", "completed", "failed"] = "running"
    current_index: int = Field(default=0, ge=0)
    outline: Artifact | None = None
    drafts: list[Artifact] = Field(default_factory=list)
    history: list[Artifact] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    pending: Artifact | None = None
    feedback: str = ""
    last_action: Literal["approve", "revise"] | None = None
    automatic_research: bool = False
    grounded_drafts: dict[str, dict] = Field(default_factory=dict)

    def approved_versions(self) -> set[tuple[str, int]]:
        return {
            (item.artifact_id, item.version) for item in self.approvals if item.action == "approve"
        }


def required_web_images(state: RunState) -> int:
    """材料规定的图片数量为硬约束；模型补出的数量仅作相关图片建议。"""
    if state.spec is None:
        return 2
    model_assessed = any(
        item.assessment.get("min_web_images", "").strip() for item in state.clarifications
    )
    if not model_assessed:
        return state.spec.min_web_images
    material_minimums = [
        int(fact.value)
        for fact in (state.analysis.facts if state.analysis else [])
        if fact.field == "min_web_images" and fact.value.isdigit()
    ]
    return max(material_minimums, default=0)

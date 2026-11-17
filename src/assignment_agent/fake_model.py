"""提供确定性输出，用于验证流程，不代表真实写作能力。"""

import json
import re

from pydantic import ValidationError

from assignment_agent.evidence_types import (
    CitationRepairs,
    CitationUse,
    CitationVerdict,
    EvidenceDraft,
    EvidenceVerdict,
    FigureUse,
    FigureVerdict,
)
from assignment_agent.outline_types import OutlineRevisionProposal, OutlineRevisionReview
from assignment_agent.research_types import ResearchPlan, ResearchSelection
from assignment_agent.schemas import (
    Analysis,
    Artifact,
    AssignmentSpec,
    ModelProfile,
    OutlinePlan,
    RequirementAssessment,
    RequirementFact,
    RunState,
    SectionPlan,
)


class FakeModel:
    profile = ModelProfile()

    @staticmethod
    def redact(text: str) -> str:
        return text

    def research_plan(self, state: RunState, targets=None) -> ResearchPlan:
        return ResearchPlan(queries=[state.spec.title[:200]])

    def select_sources(self, state, candidates, targets):
        return ResearchSelection(source_ids=[item["id"] for item in candidates[:8]])

    def repair_citations(self, state, draft, pool, indices):
        # 默认假模型不模拟证据修复；专门测试通过子类提供可控修复结果。
        return CitationRepairs()

    def repair_bindings(self, state, draft, pool, issues):
        return draft

    def revise_outline_examples(self, state, context, pool):
        return OutlineRevisionProposal(reason="默认假模型不改写大纲示例")

    def review_outline_revision(self, state, context, pool, proposal):
        return OutlineRevisionReview(
            hard_requirements_preserved=False,
            scope_preserved=False,
            evidence_supported=False,
            issues=["默认假模型不批准修订"],
        )

    def section_with_evidence(self, state, context, pool):
        targets = pool["targets"]
        sources = sorted(pool["sources"], key=lambda item: item["id"] in targets["used_sources"])
        selected = sources[: max(1, targets["new_sources"])]
        images = [item for item in pool["figures"] if item["id"] not in targets["used_figures"]][
            : targets["new_figures"]
        ]
        selected_ids = {item["id"] for item in selected}
        for figure in images:
            if figure["source_id"] not in selected_ids:
                selected.append(next(item for item in sources if item["id"] == figure["source_id"]))
                selected_ids.add(figure["source_id"])
        feedback = state.feedback.split("\n自动核验修复意见：")[0]
        if feedback.startswith("最终 Reference 核验"):
            feedback = ""
        content = self.section(
            state.model_copy(update={"pending": None, "feedback": feedback}), context
        )
        citations = []
        for source in selected:
            passage = source["passages"][0]
            quote = passage["text"].split("https://")[0][:250].strip()
            content += f"\n\n{quote} [[cite:{source['id']}]]"
            citations.append(
                CitationUse(source_id=source["id"], claim=quote, quote=quote, page=passage["page"])
            )
        figures = []
        for figure in images:
            explanation = "这张论文原图用于说明本节讨论的结构关系。"
            content += f"\n\n{explanation}\n\n[[figure:{figure['id']}]]"
            figures.append(
                FigureUse(
                    figure_id=figure["id"], caption=figure["caption"], explanation=explanation
                )
            )
        return EvidenceDraft(content=content, citations=citations, figures=figures)

    def verify_section(self, state, context, draft, pool):
        return EvidenceVerdict(
            citations=[
                CitationVerdict(index=index, supported=True, reason="假模型核验，仅用于测试")
                for index in range(len(draft.citations))
            ],
            figures=[
                FigureVerdict(
                    figure_id=use.figure_id,
                    relevant=True,
                    caption_accurate=True,
                    license_applicable=True,
                    reason="假模型核验，仅用于测试",
                )
                for use in draft.figures
            ],
        )

    def advise(self, state: RunState, question: str, history: list[dict]) -> str:
        return (
            f"假模型答疑：{question}。已参考 {len(state.materials)} 份材料；"
            "建议仅供解释，请自行确认大纲。"
        )

    def assess_requirements(
        self, state: RunState, answer: str, context: dict[str, str]
    ) -> RequirementAssessment:
        """测试替身使用固定预算验证澄清路径，不模拟真实模型判断。"""
        details = state.analysis.requirement_details.model_dump()
        title = next(
            (fact.value for fact in state.analysis.facts if fact.field == "title"), "测试作业"
        )
        word_limit = next(
            (
                int(fact.value)
                for fact in state.analysis.facts
                if fact.field == "word_limit" and fact.value.isdigit()
            ),
            (details["page_limit"] or 4) * 250,
        )
        spec = AssignmentSpec(
            **(
                details
                | {
                    "title": title,
                    "sections": [],
                    "section_mode": "recommend",
                    "word_limit": word_limit,
                    "citation_style": details["citation_style"] or "APA 7",
                    "min_sources": max(3, details["min_sources"]),
                    "min_web_images": 0,
                }
            )
        )
        return RequirementAssessment(
            spec=spec,
            reasons={
                "word_limit": "测试替身按页数给出内部写作预算",
                "citation_style": "测试替身选择 APA 7",
                "min_web_images": "未要求网络图片",
            },
        )

    def plan(self, state: RunState, context: dict[str, str]) -> OutlinePlan:
        names = (state.spec.sections or ["引言", "主体分析", "总结"])[: state.spec.word_limit]
        budget, remaining = divmod(state.spec.word_limit, len(names))
        return OutlinePlan(
            parts=[
                SectionPlan(
                    title=name,
                    word_budget=budget + (index < remaining),
                    purpose="完成当前部分论证"
                    + (f"；修改意见：{state.feedback}" if state.feedback else ""),
                    rubric_coverage=["回应题目要求"],
                    evidence_needs=["使用提供的材料"],
                )
                for index, name in enumerate(names)
            ]
        )

    def extract(self, state: RunState, context: dict[str, str]) -> Analysis:
        """仅识别测试用明确标签，其他原文保留，不宣称理解任意作业。"""
        labels = {
            "题目": "title",
            "title": "title",
            "部分": "sections",
            "sections": "sections",
            "字数上限": "word_limit",
            "word limit": "word_limit",
            "页数上限": "page_limit",
            "page limit": "page_limit",
            "页数口径": "page_count_rules",
            "引用格式": "citation_style",
            "citation style": "citation_style",
            "语言": "language",
            "作业类型": "task_type",
            "字数口径": "word_count_rules",
            "其他要求": "requirements",
            "独立引用来源最少": "min_sources",
            "网络图片最少": "min_web_images",
        }
        analysis = Analysis()
        candidates: dict[str, list] = {field: [] for field in labels.values()}
        for material in state.materials:
            if material.role == "course":
                continue
            for unit in material.units:
                for line in unit.text.splitlines():
                    match = re.fullmatch(r"\s*(?:#+\s*)?([^:：]+)[:：]\s*(.*?)\s*", line)
                    if not match or (field := labels.get(match[1].strip().lower())) is None:
                        continue
                    value = match[2]
                    analysis.facts.append(
                        RequirementFact(
                            field=field,
                            value=value,
                            source_id=material.source_id,
                            location=unit.location,
                            quote=line,
                        )
                    )
                    if field == "sections":
                        parsed = [part.strip() for part in re.split(r"[|;；、,，]", value)]
                    elif field in {"word_limit", "page_limit", "min_sources", "min_web_images"}:
                        parsed = int(value) if re.fullmatch(r"[0-9]+", value) else None
                        if parsed is not None and field in {"min_sources", "min_web_images"}:
                            parsed = max(3 if field == "min_sources" else 2, parsed)
                    elif field == "requirements":
                        parsed = [value] if value.strip() else None
                    else:
                        parsed = value.strip() or None
                    candidates[field].append(parsed)
        resolved = {}
        for field, values in candidates.items():
            if not values and field not in {"title", "sections", "word_limit"}:
                continue
            if not values or any(value is None for value in values):
                analysis.issues.append(f"{field} 缺失或标签值不明确，请确认")
            elif len({json.dumps(value, ensure_ascii=False) for value in values}) != 1:
                analysis.issues.append(f"{field} 在材料中存在冲突，请确认")
            else:
                resolved[field] = values[0]
        if not analysis.issues:
            try:
                analysis.spec = AssignmentSpec.model_validate(resolved)
            except ValidationError as exc:
                analysis.issues.append(f"提取要求不符合约束：{exc}")
        return Analysis.model_validate(analysis.model_dump(exclude={"requirement_details"}))

    def outline(self, state: RunState, context: dict[str, str]) -> str:
        sections = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(state.spec.sections))
        return f"# {state.spec.title}\n\n{sections}" + self._revision(state)

    def section(self, state: RunState, context: dict[str, str]) -> str:
        title = state.spec.sections[state.current_index]
        return f"## {title}\n\n这是《{state.spec.title}》的假模型草稿，用于验证人工确认流程。" + (
            self._revision(state)
        )

    @staticmethod
    def _revision(state: RunState) -> str:
        if not state.feedback:
            return ""
        previous: Artifact | None = state.pending
        content = f"\n\n上一版：\n{previous.content}" if previous else ""
        return f"{content}\n\n本次修改意见：{state.feedback}"

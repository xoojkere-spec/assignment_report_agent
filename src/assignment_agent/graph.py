"""按阶段保存生成、自动推进与核验结果，兼容旧检查点的确认中断。"""

from typing import TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from assignment_agent.diagnostics import emit
from assignment_agent.model_adapter import WritingModel
from assignment_agent.schemas import Approval, Artifact, Clarification, Decision, RunState
from assignment_agent.skills import active_context, load_stage
from assignment_agent.state_versions import load_run


class GraphState(TypedDict):
    # 检查点只保存基础 JSON 数据，避免绑定 Python 对象的反序列化。
    run: dict


def build_graph(model: WritingModel, checkpointer, writing=None, *, autonomous=True, database=None):
    def read_run(value):
        run = load_run(value, database) if database is not None else RunState.model_validate(value)
        # 已有检查点可以由另一真实模型续写；通过节点后将实际使用的模型写回任务。
        if run.model_profile.kind == model.profile.kind == "openai_compatible":
            run.model_profile = model.profile
        return run

    def parse(state: GraphState) -> GraphState:
        run = read_run(state["run"])
        load_stage(run, "parse")
        run.analysis = model.extract(run, active_context(run))
        fields = {fact.field for fact in run.analysis.facts}
        for field, question in (
            ("word_limit", "材料未明确字数要求；如有指定字数请说明，否则由模型评估内部写作预算"),
            ("citation_style", "材料未明确引用格式；如有指定格式请说明，否则由模型评估"),
            ("min_web_images", "材料未明确网络图片要求；如有指定数量请说明，否则由模型评估"),
        ):
            if field not in fields and question not in run.analysis.issues:
                run.analysis.issues.append(question)
        if run.analysis.spec is None and not run.analysis.issues:
            raise ValueError("解析未返回要求或待澄清问题")
        run.spec = run.analysis.spec if not run.analysis.issues else None
        if autonomous and run.spec is not None:
            run.requirements_confirmed = True
        run.status = "awaiting_user" if run.spec is None else "running"
        return {"run": run.model_dump(mode="json")}

    def clarify(state: GraphState) -> GraphState:
        run = read_run(state["run"])
        answer = Clarification.model_validate(
            interrupt({"kind": "requirements", "analysis": run.analysis.model_dump(mode="json")})
        )
        run.spec = answer.spec
        run.requirements_confirmed = True
        run.clarifications.append(answer)
        run.status = "running"
        return {"run": run.model_dump(mode="json")}

    def generate(state: GraphState, *, outline: bool, config=None) -> GraphState:
        run = read_run(state["run"])
        if run.spec is None:
            raise ValueError("要求尚未澄清，不能生成内容")
        load_stage(run, "outline" if outline else "section")
        previous = (
            run.outline
            if outline
            else next(
                (
                    item
                    for item in run.drafts
                    if item.artifact_id == f"section-{run.current_index + 1}"
                ),
                None,
            )
        )
        version = previous.version + 1 if previous else 1
        if not outline and writing and writing.enabled(run):
            if previous and run.pending is None:
                run.pending = previous
            content, bundle = writing.write(
                config["configurable"]["thread_id"], run, active_context(run), version
            )
            run.grounded_drafts[f"section-{run.current_index + 1}"] = bundle
        elif outline and run.spec.section_mode == "recommend":
            run.outline_plan = model.plan(run, active_context(run))
            content = run.outline_plan.to_markdown(run.spec.title)
        else:
            content = (
                model.outline(run, active_context(run))
                if outline
                else model.section(run, active_context(run))
            )
        artifact = Artifact(
            artifact_id="outline" if outline else f"section-{run.current_index + 1}",
            version=version,
            content=content,
        )
        if outline:
            run.outline = artifact
        elif previous:
            run.drafts[run.current_index] = artifact
        else:
            run.drafts.append(artifact)
        run.history.append(artifact)
        run.pending = artifact
        run.feedback = ""
        run.status = "running" if autonomous else "awaiting_user"
        return {"run": run.model_dump(mode="json")}

    def review(state: GraphState) -> GraphState:
        run = read_run(state["run"])
        if run.pending is None:
            raise ValueError("缺少待确认内容")
        # 此节点在恢复时重跑；中断前不生成内容、不写外部文件。
        if autonomous:
            decision = Decision(
                interrupt_id=f"auto:{run.pending.artifact_id}:{run.pending.version}",
                artifact_id=run.pending.artifact_id,
                version=run.pending.version,
                action="approve",
                feedback="系统自动推进；非人工确认。正文须已通过引用与插图核验。",
            )
            emit("review.automatic", artifact_id=decision.artifact_id, version=decision.version)
        else:
            decision = Decision.model_validate(interrupt(run.pending.model_dump()))
        if (decision.artifact_id, decision.version) != (
            run.pending.artifact_id,
            run.pending.version,
        ):
            raise ValueError("确认对象或版本已过期")
        run.approvals.append(Approval(**decision.model_dump()))
        run.last_action = decision.action
        run.feedback = decision.feedback
        run.status = "running"
        if decision.action == "approve":
            if run.pending.artifact_id == "outline" and run.outline_plan:
                # 只有批准规划后，推荐章节才成为实际写作顺序。
                run.spec.sections = [part.title for part in run.outline_plan.parts]
            elif run.pending.artifact_id != "outline":
                run.current_index += 1
                if run.automatic_research:
                    approved = run.approved_versions()
                    # 最终核验回退只替换失败稿，后续已批准版本仍交给最终核验。
                    while run.current_index < len(run.drafts):
                        draft = run.drafts[run.current_index]
                        if (draft.artifact_id, draft.version) not in approved:
                            break
                        run.current_index += 1
            run.pending = None
        return {"run": run.model_dump(mode="json")}

    def after_outline(state: GraphState) -> str:
        return "outline" if state["run"]["last_action"] == "revise" else "section"

    def after_section(state: GraphState) -> str:
        run = read_run(state["run"])
        return "finish" if run.current_index == len(run.spec.sections) else "section"

    def finish(state: GraphState, config: RunnableConfig) -> GraphState:
        run = read_run(state["run"])
        load_stage(run, "finish")
        approved = run.approved_versions()
        artifacts = [run.outline, *run.drafts]
        if len(run.drafts) != len(run.spec.sections) or any(
            item is None or (item.artifact_id, item.version) not in approved for item in artifacts
        ):
            raise ValueError("仍有未确认内容，不能结束演示流程")
        if writing and writing.enabled(run):
            index, issues = writing.audit(
                config["configurable"]["thread_id"], run, active_context(run)
            )
            if index is not None:
                # 引用失效时保留旧版与批准记录；修复稿使用新版本并重新等待章节确认。
                run.current_index = index
                run.grounded_drafts.pop(f"section-{index + 1}", None)
                run.feedback = "最终 Reference 核验未通过，自动重查并重写：" + "；".join(issues)
                run.pending = None
                run.status = "running"
                return {"run": run.model_dump(mode="json")}
        run.status = "completed"
        return {"run": run.model_dump(mode="json")}

    def generate_section(state: GraphState, config: RunnableConfig) -> GraphState:
        return generate(state, outline=False, config=config)

    graph = StateGraph(GraphState)
    graph.add_node("parse", parse)
    graph.add_node("clarify", clarify)
    graph.add_node("outline", lambda state: generate(state, outline=True))
    graph.add_node("review_outline", review)
    graph.add_node("section", generate_section)
    graph.add_node("review_section", review)
    graph.add_node("finish", finish)
    graph.add_conditional_edges(
        START,
        lambda state: (
            "parse"
            if state["run"].get("materials") and not state["run"].get("requirements_confirmed")
            else "outline"
        ),
    )
    graph.add_conditional_edges(
        "parse", lambda state: "clarify" if state["run"]["spec"] is None else "outline"
    )
    graph.add_edge("clarify", "outline")
    graph.add_edge("outline", "review_outline")
    graph.add_conditional_edges("review_outline", after_outline)
    graph.add_edge("section", "review_section")
    graph.add_conditional_edges("review_section", after_section)
    graph.add_conditional_edges(
        "finish", lambda state: END if state["run"]["status"] == "completed" else "section"
    )
    return graph.compile(checkpointer=checkpointer)

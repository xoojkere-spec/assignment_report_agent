"""管理本地单用户任务及 SQLite 连接生命周期。"""

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from assignment_agent import advisor
from assignment_agent.delivery import confirm_delivery, delivery_status
from assignment_agent.diagnostics import emit, read_diagnostics, trace_run
from assignment_agent.fake_model import FakeModel
from assignment_agent.graph import build_graph
from assignment_agent.grounded_writing import AutomaticWriting, EvidenceFailure
from assignment_agent.materials import read_material
from assignment_agent.migrations.runtime import access
from assignment_agent.model_adapter import ModelError, WritingModel
from assignment_agent.outline_revision import restore_revisions
from assignment_agent.research_budget import SearchBudget
from assignment_agent.research_store import ResearchStore, context_hash
from assignment_agent.research_types import LibraryReview, ResearchError, ResearchRequest
from assignment_agent.schemas import (
    AdvisorQuestion,
    AssignmentSpec,
    Clarification,
    Decision,
    RequirementReply,
    RunState,
)
from assignment_agent.skills import DEFAULT_SKILL_ROOT, active_context, snapshot_skill
from assignment_agent.state_versions import load_run


class AgentService:
    def __init__(
        self, graph, snapshot_directory: Path, model: WritingModel, database: Path, autonomous=True
    ):
        self.graph = graph
        self.snapshot_directory = snapshot_directory
        self.model = model
        self.database = database
        self.autonomous = autonomous

    def _load_state(self, value):
        return load_run(value, self.database)

    def _invoke(self, run_id, value):
        with trace_run(self.database, run_id):
            try:
                self.graph.invoke(value, self._config(run_id))
                completed = self._load_state(
                    self.graph.get_state(self._config(run_id)).values["run"]
                )
                if completed.status == "completed" and AutomaticWriting.enabled(completed):
                    ResearchStore(self.database).cleanup_candidates(run_id, context_hash(completed))
                if self.autonomous:
                    if completed.status == "completed":
                        pipeline = AutomaticWriting(self.database, self.model)
                        pipeline.assert_exportable(run_id, completed)
                        from assignment_agent.delivery import content_hash

                        confirm_delivery(self.database, run_id, completed, content_hash(completed))
                        emit("delivery.automatic", content_hash=content_hash(completed))
            except Exception as exc:
                # 不另写图状态，避免覆盖 LangGraph 失败节点的恢复游标。
                emit(
                    "run.failed",
                    code="model_error" if isinstance(exc, ModelError) else "execution_error",
                )
                store = ResearchStore(self.database)
                progress = store.run(run_id)
                if progress and progress.get("automatic"):
                    store.cleanup_candidates(
                        run_id, progress["context_hash"], preserve_pending=True
                    )
                raise

    @staticmethod
    def _config(run_id: str) -> dict:
        if not run_id.strip():
            raise ValueError("任务 ID 不能为空")
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 100}

    def start(
        self,
        spec: AssignmentSpec,
        run_id: str | None = None,
        skill_root: str | Path = DEFAULT_SKILL_ROOT,
    ) -> dict:
        return self._start(RunState(spec=spec, requirements_confirmed=True), run_id, skill_root)

    def replan(self, source_id: str, run_id: str | None = None) -> dict:
        source = RunState.model_validate(self.status(source_id)["state"])
        if source.spec is None:
            raise ValueError("请先澄清作业要求，再重新规划章节")
        self._check_model(self.status(source_id))
        spec = source.spec.model_copy(update={"sections": [], "section_mode": "recommend"})
        run = RunState(
            spec=spec,
            requirements_confirmed=True,
            parent_run_id=source_id,
            materials=source.materials,
            analysis=source.analysis,
            clarifications=source.clarifications,
        )
        return self._start(run, run_id, source.skill_root or DEFAULT_SKILL_ROOT)

    def start_materials(
        self,
        assignment: str | Path,
        rubrics: list | None = None,
        courses: list | None = None,
        run_id: str | None = None,
        skill_root: str | Path = DEFAULT_SKILL_ROOT,
    ) -> dict:
        inputs = [(assignment, "assignment")]
        inputs += [(path, "rubric") for path in rubrics or []]
        inputs += [(path, "course") for path in courses or []]
        if len(inputs) > 20:
            raise ValueError("单次最多导入 20 份材料")
        materials = [read_material(path, role) for path, role in inputs]
        if sum(len(unit.text) for item in materials for unit in item.units) > 500_000:
            raise ValueError("材料总文本超过 500000 字符，未截断")
        return self._start(RunState(materials=materials), run_id, skill_root)

    def _start(self, run: RunState, run_id: str | None, skill_root: str | Path) -> dict:
        run_id = run_id or str(uuid4())
        config = self._config(run_id)
        if self.graph.get_state(config).values:
            raise ValueError("任务 ID 已存在，请查看或恢复已有任务")
        run.model_profile = self.model.profile
        run.automatic_research = run.model_profile.kind == "openai_compatible" or bool(
            getattr(self.model, "automatic_research", False)
        )
        run.skill_root, run.skill_manifest = snapshot_skill(skill_root, self.snapshot_directory)
        try:
            self._invoke(run_id, {"run": run.model_dump(mode="json")})
        except ModelError as exc:
            raise ModelError(f"{exc}；任务 ID：{run_id}，可使用 resume 恢复") from None
        return self.status(run_id)

    def status(self, run_id: str) -> dict:
        snapshot = self.graph.get_state(self._config(run_id))
        if not snapshot.values:
            raise ValueError("任务不存在")
        pending = [item for task in snapshot.tasks for item in task.interrupts]
        state = self._load_state(snapshot.values["run"])
        store = ResearchStore(self.database)
        revisions = restore_revisions(store, run_id, state)
        failed_nodes = [task.name for task in snapshot.tasks if task.error]
        # 状态从当前检查点执行结果推导，重启后也能显示失败，恢复成功即自动消除。
        if failed_nodes:
            state.status = "failed"
        if state.status == "completed" and state.automatic_research:
            try:
                AutomaticWriting(self.database, self.model).assert_exportable(run_id, state)
            except EvidenceFailure:
                state.status = "failed"
                failed_nodes = ["reference_integrity"]
        current = {
            "run_id": run_id,
            "autonomous": self.autonomous,
            "state": state.model_dump(mode="json"),
            "failed_nodes": failed_nodes,
            "diagnostics": read_diagnostics(self.database, run_id),
            "delivery": delivery_status(self.database, run_id, state),
            "interrupt_id": pending[0].id if pending else None,
            "interrupt_payload": pending[0].value if pending else None,
            "next_nodes": list(snapshot.next),
            "outline_revisions": revisions,
            "effective_outline": (
                state.outline_plan.to_markdown(state.spec.title).replace(
                    "确认此规划后，将从第一章开始起草正文。",
                    "当前执行大纲已按核验证据自动修订示例；原始大纲与修订记录保留。",
                )
                if state.outline_plan and any(r["status"] == "outline_revised" for r in revisions)
                else None
            ),
        }
        current["advisor"] = {
            "available": not self.autonomous and advisor.available(current),
            "exchanges": advisor.history(self.database, run_id),
        }
        # 默认自动网站展示正式库；旧人工审阅接口继续使用独立的内部取证视图恢复检查点。
        current["research"] = (
            store.library_summary(run_id, state)
            if self.autonomous
            else store.summary(run_id, state)
        )
        chapter = state.current_index + 1 if AutomaticWriting.enabled(state) else None
        if not state.spec or state.current_index >= len(state.spec.sections):
            chapter = None
        topup = bool(
            chapter and AutomaticWriting(self.database, self.model).targets(state)["source_topup"]
        )
        current["research"]["search_budget"] = SearchBudget(
            store, run_id, chapter, allow_chapter_overflow=topup
        ).usage()
        return current

    def collect_sources(self, run_id: str, request: ResearchRequest, *, fetcher=None, search=None):
        from assignment_agent.research import ResearchCollector

        current = self.status(run_id)
        state = RunState.model_validate(current["state"])
        if state.spec is None:
            raise ResearchError("请先确认作业要求，再检索来源")
        if AutomaticWriting.enabled(state):
            raise ResearchError("当前任务在写作时自动检索、引用和核验，无需手动提交检索词")
        store = ResearchStore(self.database)
        digest = context_hash(state)
        old = store.run(run_id)
        queries = request.queries
        with trace_run(self.database, run_id):
            if not queries and old and old["context_hash"] == digest:
                queries = old.get("queries", [])
            if not queries:
                self._check_model(current)
                try:
                    queries = self.model.research_plan(state).queries
                except ModelError as exc:
                    store.save_run(run_id, digest, status="partial", issues=[str(exc)])
                    raise
            if getattr(self.model, "_settings", None):
                queries = [self.model.redact(query) for query in queries]
            ResearchCollector(
                store, fetcher=fetcher, search=search, budget=SearchBudget(store, run_id)
            ).collect(run_id, state, queries)
        return self.status(run_id)

    def review_source(self, run_id: str, request: LibraryReview):
        state = RunState.model_validate(self.status(run_id)["state"])
        if AutomaticWriting.enabled(state):
            raise ResearchError("当前任务的来源由 AI 自动核验；请在章节审阅中提出写作修改意见")
        ResearchStore(self.database).review(run_id, state, request)
        return self.status(run_id)

    def ask(self, run_id: str, question: AdvisorQuestion) -> dict:
        current = self.status(run_id)
        if not current["advisor"]["available"] or question.interrupt_id != current["interrupt_id"]:
            raise ValueError("仅可在当前要求澄清或大纲确认时提问，请刷新任务状态")
        self._check_model(current)
        text = self.model.redact(question.question)
        exchange_id = advisor.begin(self.database, run_id, question.interrupt_id, text)
        with trace_run(self.database, run_id):
            try:
                answer = self.model.advise(
                    RunState.model_validate(current["state"]),
                    text,
                    current["advisor"]["exchanges"],
                )
            except Exception as exc:
                # 答疑失败只更新聊天记录，不触碰写作状态或确认游标。
                error = (
                    self.model.redact(str(exc))
                    if isinstance(exc, ModelError)
                    else "答疑失败，请重新提问"
                )
                advisor.finish(self.database, exchange_id, error=error)
                emit(
                    "advisor.failed",
                    code="model_error" if isinstance(exc, ModelError) else "execution_error",
                )
                raise ModelError(error) from None
        advisor.finish(self.database, exchange_id, answer=self.model.redact(answer))
        return self.status(run_id)

    def confirm_completion(self, run_id: str, expected_hash: str) -> dict:
        current = self.status(run_id)
        AutomaticWriting(self.database, self.model).assert_exportable(
            run_id, RunState.model_validate(current["state"])
        )
        confirm_delivery(
            self.database, run_id, RunState.model_validate(current["state"]), expected_hash
        )
        return self.status(run_id)

    def export_docx(self, run_id: str) -> tuple[bytes, str]:
        current = self.status(run_id)
        if not current["delivery"]["confirmed"]:
            raise ValueError("请先审阅全文并点击确认完成，再导出 DOCX")
        from assignment_agent.export_docx import build_docx

        run = RunState.model_validate(current["state"])
        pipeline = AutomaticWriting(self.database, self.model)
        pipeline.assert_exportable(run_id, run)
        images = {
            item["id"]: pipeline.store.assets / item["filename"]
            for item in current["research"]["figures"]
            if item["integrity_ok"]
        }
        return build_docx(run, images=images), run.spec.title

    def decide(self, run_id: str, decision: Decision) -> dict:
        if self.autonomous:
            raise ValueError("当前为自动写作模式，不等待人工确认；失败后可恢复任务")
        current = self.status(run_id)
        pending = current["state"]["pending"]
        if current["interrupt_id"] != decision.interrupt_id or pending is None:
            raise ValueError("确认请求已过期或任务未在等待确认，请重新查看状态")
        if (pending["artifact_id"], pending["version"]) != (decision.artifact_id, decision.version):
            raise ValueError("确认对象或版本不匹配")
        self._check_model(current)
        self._invoke(
            run_id,
            Command(resume={decision.interrupt_id: decision.model_dump(mode="json")}),
        )
        return self.status(run_id)

    def clarify(self, run_id: str, answer: Clarification) -> dict:
        current = self.status(run_id)
        payload = current["interrupt_payload"] or {}
        if current["interrupt_id"] != answer.interrupt_id or payload.get("kind") != "requirements":
            raise ValueError("澄清请求已过期或任务未在等待澄清")
        self._check_model(current)
        # 旧客户端或 CLI 可能只提交基础字段；未提交不等于清除已提取要求。
        details = current["state"]["analysis"]["requirement_details"]
        answer = answer.model_copy(
            update={
                "spec": AssignmentSpec.model_validate(
                    details | answer.spec.model_dump(exclude_unset=True)
                )
            }
        )
        self._invoke(
            run_id,
            Command(resume={answer.interrupt_id: answer.model_dump(mode="json")}),
        )
        return self.status(run_id)

    def answer_requirements(self, run_id: str, reply: RequirementReply) -> dict:
        current = self.status(run_id)
        payload = current["interrupt_payload"] or {}
        if current["interrupt_id"] != reply.interrupt_id or payload.get("kind") != "requirements":
            raise ValueError("澄清请求已过期或任务未在等待澄清")
        self._check_model(current)
        state = RunState.model_validate(current["state"])
        with trace_run(self.database, run_id):
            assessment = self.model.assess_requirements(state, reply.answer, active_context(state))
        missing = {"word_limit", "citation_style", "min_web_images"} - {
            fact.field for fact in state.analysis.facts
        }
        if any(not assessment.reasons.get(field, "").strip() for field in missing):
            raise ValueError("模型未说明缺失要求的评估依据")
        known = state.analysis.requirement_details
        if known.page_limit is not None and assessment.spec.page_limit != known.page_limit:
            raise ValueError("模型评估未保留材料中的页数上限")
        if known.page_count_rules:
            # 材料中的页数口径已确定，模型漏填或同义改写时沿用原值。
            assessment.spec.page_count_rules = known.page_count_rules
        if known.citation_style and assessment.spec.citation_style != known.citation_style:
            raise ValueError("模型评估未保留材料中的引用格式")
        for fact in state.analysis.facts:
            if fact.field == "word_limit" and fact.value.isdigit():
                if assessment.spec.word_limit != int(fact.value):
                    raise ValueError("模型评估未保留材料中的字数上限")
            if fact.field in {"min_sources", "min_web_images"} and fact.value.isdigit():
                if getattr(assessment.spec, fact.field) < int(fact.value):
                    raise ValueError("模型评估低于材料中的来源或图片数量要求")
        answer = Clarification(
            interrupt_id=reply.interrupt_id,
            spec=assessment.spec,
            note=reply.answer,
            assessment=assessment.reasons,
        )
        self._invoke(
            run_id,
            Command(resume={reply.interrupt_id: answer.model_dump(mode="json")}),
        )
        return self.status(run_id)

    def recover(self, run_id: str) -> dict:
        current = self.status(run_id)
        if (
            self.autonomous
            and current["state"]["status"] == "completed"
            and not current["delivery"]["confirmed"]
        ):
            return self.confirm_completion(run_id, current["delivery"]["content_hash"])
        if current["failed_nodes"] == ["reference_integrity"]:
            self._check_model(current)
            run = RunState.model_validate(current["state"])
            run.status = "running"
            # 完成后发现缓存失效时回到全文核验，而不是绕过检查直接导出旧版。
            self.graph.update_state(
                self._config(run_id), {"run": run.model_dump(mode="json")}, as_node="review_section"
            )
            self._invoke(run_id, None)
            return self.status(run_id)
        if self.autonomous and current["interrupt_id"]:
            if (current["interrupt_payload"] or {}).get("kind") == "requirements":
                return current
            self._check_model(current)
            pending = current["state"]["pending"]
            # 消费旧版本留下的中断；新图由自动核验节点继续，不伪造人工批准。
            resumed = (
                {
                    "interrupt_id": current["interrupt_id"],
                    "artifact_id": pending["artifact_id"],
                    "version": pending["version"],
                    "action": "approve",
                    "feedback": "迁移至自动流程",
                }
                if pending
                else {}
            )
            self._invoke(
                run_id,
                Command(resume={current["interrupt_id"]: resumed}),
            )
            return self.status(run_id)
        if current["interrupt_id"] is None and current["next_nodes"]:
            self._check_model(current)
            self._invoke(run_id, None)
        return self.status(run_id)

    def _check_model(self, current: dict) -> None:
        saved = RunState.model_validate(current["state"]).model_profile
        if saved.kind != self.model.profile.kind:
            raise ModelError("当前模型类型与任务记录不兼容，不能用测试模型接管真实模型任务")


@contextmanager
def open_agent(database: str | Path, model: WritingModel | None = None, *, autonomous=True):
    path = Path(database)
    with access(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        selected = model if model is not None else FakeModel()
        selected.autonomous = autonomous
        with SqliteSaver.from_conn_string(str(path)) as saver:
            yield AgentService(
                build_graph(
                    selected,
                    saver,
                    AutomaticWriting(path, selected),
                    autonomous=autonomous,
                    database=path,
                ),
                path.parent / "skill-snapshots",
                selected,
                path,
                autonomous,
            )

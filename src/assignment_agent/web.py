"""本地单用户工作台：复用 Agent 服务，串行执行耗时任务。"""

import argparse
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Annotated
from urllib.parse import quote
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from assignment_agent.advisor import interrupt_pending
from assignment_agent.materials import MAX_FILE_BYTES
from assignment_agent.migrations.runtime import access
from assignment_agent.model_adapter import (
    APISettings,
    ModelError,
    OpenAICompatibleModel,
    read_settings,
)
from assignment_agent.research_store import ResearchStore, interrupt_research
from assignment_agent.research_types import LibraryReview, ResearchRequest
from assignment_agent.schemas import (
    AdvisorQuestion,
    Clarification,
    CompletionRequest,
    Decision,
    RequirementReply,
)
from assignment_agent.service import open_agent as service_agent

STATIC = Path(__file__).parent / "static"


def view(result: dict) -> dict:
    state = dict(result["state"])
    for key in ("skill_documents", "skill_manifest", "skill_root"):
        state.pop(key, None)
    state["materials"] = [{**item, "path": Path(item["path"]).name} for item in state["materials"]]
    return {**result, "state": state}


def create_app(
    database="workspace/agent.sqlite",
    env_file=".env",
    uploads="workspace/uploads",
    *,
    autonomous=True,
):
    def open_agent(database, model=None):
        return service_agent(database, model, autonomous=autonomous)

    executor = ThreadPoolExecutor(max_workers=1)
    guard = threading.Lock()
    jobs: dict[str, dict] = {}
    settings: APISettings | None = None

    @asynccontextmanager
    async def lifespan(app):
        with access(database):
            interrupt_pending(database)
            interrupt_research(database)
            ResearchStore(database).cleanup_interrupted()
        yield
        executor.shutdown(wait=True)

    app = FastAPI(title="作业工坊", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def local_request(request: Request, call_next):
        # 自定义头配合同源限制，阻止其他网站通过普通表单操作本地服务。
        if request.method in {"POST", "PUT", "DELETE"}:
            if request.headers.get("x-assignment-client") != "local":
                return JSONResponse({"detail": "请通过本地工作台执行此操作"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
                return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # 默认验证响应会附带原始输入，配置请求中可能包含密钥。
        return JSONResponse({"detail": "输入格式无效，请检查必填字段及数值"}, status_code=422)

    @app.exception_handler(ValueError)
    async def bad_value(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def model():
        return OpenAICompatibleModel(settings or read_settings(env_file))

    def submit(run_id, operation, selected, kind="writing"):
        with guard:
            if any(job["status"] in {"queued", "running"} for job in jobs.values()):
                raise HTTPException(409, "已有任务正在处理，请稍后再试")
            job_id = str(uuid4())
            jobs[job_id] = {
                "job_id": job_id,
                "run_id": run_id,
                "status": "queued",
                "error": None,
                "kind": kind,
            }

        def execute():
            jobs[job_id]["status"] = "running"
            try:
                with open_agent(database, selected) as agent:
                    operation(agent)
                jobs[job_id]["status"] = "completed"
            except (ValueError, OSError) as exc:
                message = str(exc)
                if selected._settings:
                    message = message.replace(
                        selected.settings.api_key.get_secret_value(), "[REDACTED]"
                    )
                jobs[job_id].update(status="failed", error=message)
            except Exception:
                jobs[job_id].update(status="failed", error="处理失败，请检查材料后恢复任务")

        executor.submit(execute)
        return dict(jobs[job_id])

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def get_config():
        try:
            current = settings or read_settings(env_file)
            return {
                "configured": True,
                "base_url": current.base_url,
                "model": current.model,
                "timeout": current.timeout,
                "max_input_chars": current.max_input_chars,
                "source": "session" if settings else "env",
            }
        except ModelError:
            return {
                "configured": False,
                "base_url": "",
                "model": "",
                "timeout": APISettings.model_fields["timeout"].default,
                "max_input_chars": APISettings.model_fields["max_input_chars"].default,
                "source": None,
            }

    @app.post("/api/config")
    def set_config(config: APISettings):
        nonlocal settings
        settings = config.checked()
        return get_config()

    @app.delete("/api/config")
    def clear_config():
        nonlocal settings
        settings = None
        return get_config()

    @app.get("/api/runs")
    def list_runs():
        summaries = []
        seen = set()
        with open_agent(database) as agent:
            # 使用检查点公开列表接口，可同时发现命令行创建的任务。
            # 列表生成器持有 SQLite 锁，先收集标识并释放，再读取最新状态。
            references = []
            with closing(agent.graph.checkpointer.list(None)) as checkpoints:
                for checkpoint in checkpoints:
                    run_id = checkpoint.config["configurable"]["thread_id"]
                    if run_id in seen:
                        continue
                    seen.add(run_id)
                    references.append((run_id, checkpoint.checkpoint["ts"]))
                    if len(references) == 50:
                        break
            for run_id, updated_at in references:
                result = agent.status(run_id)
                state = result["state"]
                summaries.append(
                    {
                        "run_id": run_id,
                        "title": (state["spec"] or {}).get(
                            "title", "解析失败" if state["status"] == "failed" else "正在解析作业"
                        ),
                        "status": state["status"],
                        "delivery_confirmed": result["delivery"]["confirmed"],
                        "current_index": state["current_index"],
                        "sections": len((state.get("outline_plan") or {}).get("parts", []))
                        or len((state["spec"] or {}).get("sections", [])),
                        "updated_at": updated_at,
                    }
                )
        return {"runs": summaries, "jobs": list(jobs.values())[-50:]}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        with open_agent(database) as agent:
            return view(agent.status(run_id))

    @app.post("/api/runs", status_code=202)
    def start_run(
        assignment: Annotated[UploadFile, File()],
        rubrics: Annotated[list[UploadFile] | None, File()] = None,
        courses: Annotated[list[UploadFile] | None, File()] = None,
    ):
        rubrics = rubrics or []
        courses = courses or []
        selected = model()
        files = [assignment, *rubrics, *courses]
        if len(files) > 20:
            raise HTTPException(400, "每次最多上传 20 份材料")
        run_id = str(uuid4())
        folder = Path(uploads) / run_id
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, upload in enumerate(files):
            name = Path((upload.filename or "file").replace("\\", "/")).name
            name = re.sub(r"[^\w.\-]", "_", name)[:120]
            if Path(name).suffix.lower() not in {".pdf", ".docx", ".txt", ".md"}:
                raise HTTPException(400, "仅支持 PDF、DOCX、TXT 和 Markdown")
            raw = upload.file.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise HTTPException(400, "单个文件不能超过 20 MiB")
            path = folder / f"{index}-{name}"
            path.write_bytes(raw)
            paths.append(path)
        return submit(
            run_id,
            lambda agent: agent.start_materials(
                paths[0], paths[1 : 1 + len(rubrics)], paths[1 + len(rubrics) :], run_id=run_id
            ),
            selected,
        )

    @app.post("/api/runs/{run_id}/decision", status_code=202)
    def decide(run_id: str, decision: Decision):
        return submit(run_id, lambda agent: agent.decide(run_id, decision), model())

    @app.post("/api/runs/{run_id}/advisor", status_code=202)
    def ask(run_id: str, question: AdvisorQuestion):
        with open_agent(database) as agent:
            current = agent.status(run_id)
        if not current["advisor"]["available"] or question.interrupt_id != current["interrupt_id"]:
            raise ValueError("仅可在当前要求澄清或大纲确认时提问，请刷新任务状态")
        return submit(run_id, lambda agent: agent.ask(run_id, question), model(), kind="advisor")

    @app.post("/api/runs/{run_id}/research", status_code=202)
    def research(run_id: str, request: ResearchRequest):
        with open_agent(database) as agent:
            current = agent.status(run_id)
        if not current["research"]["available"]:
            raise ValueError("请先确认作业要求，再检索来源")
        manual_or_resume = request.queries or (
            current["research"]["queries"] and not current["research"]["stale"]
        )
        selected = OpenAICompatibleModel(settings) if manual_or_resume else model()
        return submit(
            run_id, lambda agent: agent.collect_sources(run_id, request), selected, kind="research"
        )

    @app.post("/api/runs/{run_id}/research/review")
    def review_research(run_id: str, request: LibraryReview):
        with guard:
            if any(job["status"] in {"queued", "running"} for job in jobs.values()):
                raise HTTPException(409, "检索或生成正在进行，请稍后审阅来源")
            with open_agent(database) as agent:
                return view(agent.review_source(run_id, request))

    @app.get("/api/runs/{run_id}/research/assets/{entry_id}")
    def research_asset(run_id: str, entry_id: str):
        with open_agent(database) as agent:
            library = agent.status(run_id)["research"]
        store = ResearchStore(database)
        figure = next((item for item in library["figures"] if item["id"] == entry_id), None)
        source = next((item for item in library["sources"] if item["id"] == entry_id), None)
        asset = figure or (source or {}).get("pdf")
        if not asset or not store.asset_valid(asset):
            raise HTTPException(404, "来源文件不存在或已变化，请恢复检索")
        if figure:
            return FileResponse(store.assets / asset["filename"], media_type="image/png")
        return FileResponse(
            store.assets / asset["filename"],
            media_type="application/pdf",
            filename="source-paper.pdf",
        )

    @app.post("/api/runs/{run_id}/clarify", status_code=202)
    def clarify(run_id: str, answer: Clarification):
        return submit(run_id, lambda agent: agent.clarify(run_id, answer), model())

    @app.post("/api/runs/{run_id}/requirements-answer", status_code=202)
    def answer_requirements(run_id: str, answer: RequirementReply):
        return submit(run_id, lambda agent: agent.answer_requirements(run_id, answer), model())

    @app.post("/api/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str):
        return submit(run_id, lambda agent: agent.recover(run_id), model())

    @app.post("/api/runs/{run_id}/replan", status_code=202)
    def replan(run_id: str):
        new_id = str(uuid4())
        return submit(new_id, lambda agent: agent.replan(run_id, new_id), model())

    @app.post("/api/runs/{run_id}/complete")
    def complete(run_id: str, request: CompletionRequest):
        # 全文确认不调用模型，因此不要求重新提供 Key。
        with open_agent(database) as agent:
            return view(agent.confirm_completion(run_id, request.content_hash))

    @app.get("/api/runs/{run_id}/export.docx")
    def export(run_id: str):
        with open_agent(database) as agent:
            content, title = agent.export_docx(run_id)
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:90] or "assignment"
        return Response(
            content,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={
                "Content-Disposition": 'attachment; filename="assignment.docx"; '
                f"filename*=UTF-8''{quote(filename + '.docx', safe='')}"
            },
        )

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        if job_id not in jobs:
            raise HTTPException(404, "操作记录不存在；请查看任务状态或恢复任务")
        return dict(jobs[job_id])

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main():
    # 保持 Windows 终端和管道中的中文启动信息一致。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="启动作业工坊本地网站")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="workspace/agent.sqlite")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    uvicorn.run(create_app(args.db, args.env_file), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

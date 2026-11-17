"""提供真实本地 HTTP 传输的兼容 API 测试服务，不访问外部模型。"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from _test_isolation import MODEL_VARIABLES, PROXY_VARIABLES, install

from assignment_agent.fake_model import FakeModel
from assignment_agent.schemas import Artifact, AssignmentSpec, Material, RunState


def pytest_sessionstart(session):
    # 收集测试前生效，避免模块导入时绕过网络保护。
    install()


@pytest.fixture(autouse=True)
def isolated_test_environment(monkeypatch):
    for name in (*MODEL_VARIABLES, *PROXY_VARIABLES):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    for name in ("LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING"):
        monkeypatch.setenv(name, "false")
    tests = Path(__file__).resolve().parent
    # CLI 自己的启动钩子仍可排在最前面，随后显式调用同一保护入口。
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(tests / "_bootstrap"), str(tests))))


def legacy_agent(*args, **kwargs):
    """旧检查点的人工确认接口仍需回归验证；新默认自动流程由独立端到端用例覆盖。"""
    from assignment_agent.service import open_agent

    kwargs.setdefault("autonomous", False)
    return open_agent(*args, **kwargs)


def legacy_app(*args, **kwargs):
    from assignment_agent.web import create_app

    kwargs.setdefault("autonomous", False)
    return create_app(*args, **kwargs)


@pytest.fixture
def compatible_server(monkeypatch):
    from test_research import research_network

    from assignment_agent import research
    from assignment_agent.evidence_types import EvidenceDraft

    _, _, public_fetcher = research_network.__wrapped__()
    monkeypatch.setattr(research, "PublicFetcher", public_fetcher)
    requests = []
    replies = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(
                {"path": self.path, "authorization": self.headers["Authorization"], "body": body}
            )
            if replies:
                status, content = replies.pop(0)
            else:
                payload = json.loads(body["messages"][-1]["content"])
                if payload["task"] == "extract":
                    materials = [
                        Material.model_validate({**item, "path": "test"})
                        for item in payload["materials"]
                    ]
                    content = (
                        FakeModel().extract(RunState(materials=materials), {}).model_dump_json()
                    )
                elif payload["task"] == "plan":
                    state = RunState(spec=AssignmentSpec.model_validate(payload["spec"]))
                    content = FakeModel().plan(state, {}).model_dump_json()
                elif payload["task"] == "advisor":
                    content = "答疑解释：" + payload["question"]
                elif payload["task"] == "research_plan":
                    content = json.dumps({"queries": [payload["spec"]["title"]]})
                elif payload["task"] == "source_selection":
                    content = json.dumps(
                        {"source_ids": [item["id"] for item in payload["candidates"][:8]]}
                    )
                elif payload["task"] in {"grounded_section", "verify_section"}:
                    spec = AssignmentSpec.model_validate(payload["spec"])
                    state = RunState(
                        spec=spec,
                        current_index=spec.sections.index(payload["current_section"]),
                        feedback=payload["feedback"],
                        outline=Artifact(
                            artifact_id="outline", version=1, content=payload["approved_outline"]
                        ),
                    )
                    model = FakeModel()
                    pool = payload["evidence_pool"]
                    content = (
                        model.section_with_evidence(state, {}, pool).model_dump_json()
                        if payload["task"] == "grounded_section"
                        else model.verify_section(
                            state,
                            {},
                            EvidenceDraft.model_validate(payload["draft_to_verify"]),
                            pool,
                        ).model_dump_json()
                    )
                else:
                    content = (
                        "真实协议测试草稿："
                        + payload["task"]
                        + "\n"
                        + (payload["current_section"] or "大纲")
                        + "\n"
                        + payload["feedback"]
                    )
                status = 200
            wire = (
                json.dumps(
                    {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]},
                    ensure_ascii=False,
                )
                if status == 200
                else content
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, replies
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

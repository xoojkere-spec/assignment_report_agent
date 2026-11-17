import json
import os
import subprocess
import sys
import time
from io import BytesIO

import pytest
from conftest import legacy_app as create_app
from docx import Document
from fastapi.testclient import TestClient

HEADERS = {"X-Assignment-Client": "local"}


def test_web_launcher_help_uses_utf8():
    result = subprocess.run(
        [sys.executable, "-m", "assignment_agent.web", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp936"},
    )
    assert "启动作业工坊本地网站" in result.stdout


def wait(client, job):
    for _ in range(300):
        current = client.get("/api/jobs/" + job["job_id"]).json()
        if current["status"] in {"completed", "failed"}:
            return current
        time.sleep(0.01)
    pytest.fail("后台任务未在预期时间完成")


def config(client, url):
    return client.post(
        "/api/config",
        headers=HEADERS,
        json={
            "base_url": url,
            "model": "web-test",
            "api_key": "web-private-key",
        },
    )


def test_web_session_input_limit_defaults_high_and_can_be_changed(tmp_path):
    app = create_app(tmp_path / "limit.sqlite", tmp_path / "missing.env", tmp_path / "uploads")
    with TestClient(app) as client:
        assert client.get("/api/config").json()["max_input_chars"] == 300_000
        payload = {
            "base_url": "https://model.example/v1",
            "model": "long-context",
            "api_key": "web-private-key",
        }
        default = client.post("/api/config", headers=HEADERS, json=payload)
        assert default.status_code == 200
        assert default.json()["max_input_chars"] == 300_000
        custom = client.post(
            "/api/config", headers=HEADERS, json={**payload, "max_input_chars": 400_000}
        )
        assert custom.status_code == 200
        assert client.get("/api/config").json()["max_input_chars"] == 400_000
        invalid = client.post(
            "/api/config", headers=HEADERS, json={**payload, "max_input_chars": 0}
        )
        assert invalid.status_code == 422
        assert client.get("/api/config").json()["max_input_chars"] == 400_000
        assert "api_key" not in client.get("/api/config").text


def test_web_session_timeout_defaults_to_300_and_can_be_raised(tmp_path):
    app = create_app(tmp_path / "timeout.sqlite", tmp_path / "missing.env", tmp_path / "uploads")
    with TestClient(app) as client:
        assert client.get("/api/config").json()["timeout"] == 300
        payload = {
            "base_url": "https://model.example/v1",
            "model": "long-request",
            "api_key": "web-private-key",
        }
        assert client.post("/api/config", headers=HEADERS, json=payload).json()["timeout"] == 300
        raised = client.post("/api/config", headers=HEADERS, json={**payload, "timeout": 600})
        assert raised.status_code == 200
        assert client.get("/api/config").json()["timeout"] == 600
        for invalid in (0, 1801):
            assert (
                client.post(
                    "/api/config", headers=HEADERS, json={**payload, "timeout": invalid}
                ).status_code
                == 422
            )
            assert client.get("/api/config").json()["timeout"] == 600


def test_web_workflow_upload_review_and_restore(tmp_path, compatible_server):
    url, _, _ = compatible_server
    db = tmp_path / "state.sqlite"
    app = create_app(db, tmp_path / "missing.env", tmp_path / "uploads")
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/config").json()["configured"] is False
        assert config(client, url).status_code == 200
        assert "api_key" not in client.get("/api/config").text
        response = client.post(
            "/api/runs",
            headers=HEADERS,
            files={
                "assignment": (
                    "brief.txt",
                    (
                        "题目：网页测试\n部分：分析；结论\n字数上限：500\n"
                        "引用格式：APA 7\n网络图片最少：2"
                    ).encode(),
                ),
            },
        )
        assert response.status_code == 202
        job = wait(client, response.json())
        assert job["status"] == "completed", job
        run_id = job["run_id"]
        result = client.get("/api/runs/" + run_id).json()
        assert result["state"]["outline"]
        assert "skill_documents" not in result["state"]
        assert client.get("/api/runs").json()["runs"][0]["run_id"] == run_id
        for action in ("revise", "approve", "approve", "approve"):
            pending = result["state"]["pending"]
            response = client.post(
                f"/api/runs/{run_id}/decision",
                headers=HEADERS,
                json={
                    "interrupt_id": result["interrupt_id"],
                    "artifact_id": pending["artifact_id"],
                    "version": pending["version"],
                    "action": action,
                    "feedback": "加入案例",
                },
            )
            assert wait(client, response.json())["status"] == "completed"
            result = client.get("/api/runs/" + run_id).json()
        assert result["state"]["status"] == "completed"
        assert "web-private-key" not in json.dumps(result)
    with TestClient(create_app(db, tmp_path / "missing.env", tmp_path / "uploads")) as client:
        assert client.get("/api/config").json()["configured"] is False
        assert client.get("/api/runs/" + run_id).json() == result
    assert b"web-private-key" not in db.read_bytes()


def test_web_errors_and_cross_site_protection(tmp_path, compatible_server):
    url, _, replies = compatible_server
    with TestClient(
        create_app(tmp_path / "state.sqlite", tmp_path / "missing.env", tmp_path / "uploads")
    ) as client:
        assert client.post("/api/config", json={}).status_code == 403
        assert (
            client.post(
                "/api/config", headers={**HEADERS, "Origin": "https://other.example"}, json={}
            ).status_code
            == 403
        )
        result = client.post("/api/config", headers=HEADERS, json={"api_key": "private"})
        assert result.status_code == 422 and "private" not in result.text
        assert config(client, url).status_code == 200
        assert (
            client.post(
                "/api/runs", headers=HEADERS, files={"assignment": ("old.doc", b"old")}
            ).status_code
            == 400
        )
        replies.append((500, "web-private-key"))
        job = client.post(
            "/api/runs",
            headers=HEADERS,
            files={
                "assignment": ("brief.txt", "题目：错误恢复\n部分：分析\n字数上限：500".encode())
            },
        ).json()
        failed = wait(client, job)
        assert failed["status"] == "failed"
        assert "web-private-key" not in failed["error"]
        switched = client.post(
            "/api/config",
            headers=HEADERS,
            json={"base_url": url, "model": "web-test-switched", "api_key": "web-private-key"},
        )
        assert switched.status_code == 200
        recovered = client.post(f"/api/runs/{job['run_id']}/resume", headers=HEADERS).json()
        assert wait(client, recovered)["status"] == "completed"
        assert (
            client.get(f"/api/runs/{job['run_id']}").json()["state"]["model_profile"]["model"]
            == "web-test-switched"
        )


def test_web_requirement_clarification(tmp_path, compatible_server):
    url, _, _ = compatible_server
    with TestClient(
        create_app(tmp_path / "state.sqlite", tmp_path / "missing.env", tmp_path / "uploads")
    ) as client:
        config(client, url)
        job = client.post(
            "/api/runs",
            headers=HEADERS,
            files={"assignment": ("brief.txt", "题目：缺项澄清\n部分：分析".encode())},
        ).json()
        assert wait(client, job)["status"] == "completed"
        run_id = job["run_id"]
        result = client.get("/api/runs/" + run_id).json()
        assert result["interrupt_payload"]["kind"] == "requirements"
        response = client.post(
            f"/api/runs/{run_id}/clarify",
            headers=HEADERS,
            json={
                "interrupt_id": result["interrupt_id"],
                "note": "确认字数上限为 500",
                "spec": {"title": "缺项澄清", "sections": ["分析"], "word_limit": 500},
            },
        )
        assert wait(client, response.json())["status"] == "completed"
        result = client.get("/api/runs/" + run_id).json()
        assert result["state"]["outline"] is not None
        assert result["state"]["drafts"] == []


def test_web_clarification_retains_reference_and_image_targets(tmp_path, compatible_server):
    url, requests, replies = compatible_server
    db = tmp_path / "requirements.sqlite"
    env_file = tmp_path / "missing.env"
    details = {
        "citation_style": "Harvard",
        "page_limit": None,
        "page_count_rules": None,
        "min_sources": 5,
        "min_web_images": 4,
        "language": "英文",
        "task_type": "比较报告",
        "word_count_rules": "不含参考文献和图注",
        "requirements": ["网络图片需图注与来源", "比较两个系统的架构"],
    }
    replies.append(
        (
            200,
            json.dumps(
                {
                    "issues": ["请确认字数上限"],
                    "spec": None,
                    "requirement_details": details,
                }
            ),
        )
    )
    with TestClient(create_app(db, env_file, tmp_path / "uploads")) as client:
        config(client, url)
        job = client.post(
            "/api/runs",
            headers=HEADERS,
            files={"assignment": ("brief.txt", "题目：网页要求保存\n引用格式：Harvard".encode())},
        ).json()
        assert wait(client, job)["status"] == "completed"
        run_id = job["run_id"]
        pending = client.get("/api/runs/" + run_id).json()
        assert pending["state"]["analysis"]["requirement_details"] == details
    with TestClient(create_app(db, env_file)) as client:
        assert client.get("/api/runs/" + run_id).json() == pending
        config(client, url)
        spec = {
            **details,
            "title": "网页要求保存",
            "sections": [],
            "section_mode": "recommend",
            "word_limit": 1200,
        }
        for invalid in ({"min_sources": 2}, {"min_web_images": -1}):
            response = client.post(
                f"/api/runs/{run_id}/clarify",
                headers=HEADERS,
                json={
                    "interrupt_id": pending["interrupt_id"],
                    "spec": spec | invalid,
                    "note": "确认要求",
                },
            )
            assert response.status_code == 422
        job = client.post(
            f"/api/runs/{run_id}/clarify",
            headers=HEADERS,
            json={
                "interrupt_id": pending["interrupt_id"],
                "spec": spec,
                "note": "补充字数并确认其余要求",
            },
        ).json()
        assert wait(client, job)["status"] == "completed"
        result = client.get("/api/runs/" + run_id).json()
        assert result["state"]["spec"] == spec
        assert result["state"]["clarifications"][0]["spec"] == spec
        assert result["state"]["drafts"] == []
        assert result["state"]["pending"]["artifact_id"] == "outline"
        model_input = json.loads(requests[-1]["body"]["messages"][1]["content"])
        assert model_input["spec"] == spec
    with TestClient(create_app(db, env_file)) as client:
        assert client.get("/api/runs/" + run_id).json() == result


def test_web_requirements_answer_uses_model_assessment_after_reply(tmp_path, compatible_server):
    url, requests, replies = compatible_server
    with TestClient(
        create_app(tmp_path / "requirements.sqlite", tmp_path / "missing.env", tmp_path / "uploads")
    ) as client:
        assert config(client, url).status_code == 200
        job = client.post(
            "/api/runs",
            headers=HEADERS,
            files={"assignment": ("brief.txt", "题目：SRS\n部分：分析\n页数上限：30".encode())},
        ).json()
        assert wait(client, job)["status"] == "completed"
        run_id = job["run_id"]
        waiting = client.get("/api/runs/" + run_id).json()
        assert waiting["interrupt_payload"]["kind"] == "requirements"
        assert waiting["state"]["analysis"]["requirement_details"]["page_limit"] == 30
        replies.append(
            (
                200,
                json.dumps(
                    {
                        "spec": {
                            "title": "SRS",
                            "sections": [],
                            "section_mode": "recommend",
                            "word_limit": 7500,
                            "page_limit": 30,
                            "citation_style": "APA 7",
                            "min_sources": 3,
                            "min_web_images": 0,
                        },
                        "reasons": {
                            "word_limit": "内部预算",
                            "citation_style": "用户未指定",
                            "min_web_images": "不需要网络图片",
                        },
                    }
                ),
            )
        )
        response = client.post(
            f"/api/runs/{run_id}/requirements-answer",
            headers=HEADERS,
            json={"interrupt_id": waiting["interrupt_id"], "answer": "没有额外要求"},
        )
        assert response.status_code == 202
        assert wait(client, response.json())["status"] == "completed"
        result = client.get("/api/runs/" + run_id).json()
        assert result["state"]["spec"]["page_limit"] == 30
        assert result["state"]["spec"]["min_web_images"] == 0
        assert result["state"]["clarifications"][-1]["assessment"]["word_limit"] == "内部预算"
        assert any(
            json.loads(item["body"]["messages"][1]["content"]).get("user_answer") == "没有额外要求"
            for item in requests
        )


def test_web_replan_confirm_and_write_multiple_chapters(tmp_path, compatible_server):
    url, requests, _ = compatible_server
    with TestClient(
        create_app(tmp_path / "state.sqlite", tmp_path / "missing.env", tmp_path / "uploads")
    ) as client:
        config(client, url)
        job = client.post(
            "/api/runs",
            headers=HEADERS,
            files={
                "assignment": (
                    "brief.txt",
                    (
                        "题目：章节写作\n部分：旧部分\n字数上限：900\n"
                        "引用格式：APA 7\n网络图片最少：2"
                    ).encode(),
                )
            },
        ).json()
        assert wait(client, job)["status"] == "completed"
        original_id = job["run_id"]
        original = client.get("/api/runs/" + original_id).json()
        job = client.post(f"/api/runs/{original_id}/replan", headers=HEADERS).json()
        assert wait(client, job)["status"] == "completed"
        run_id = job["run_id"]
        result = client.get("/api/runs/" + run_id).json()
        assert len(result["state"]["outline_plan"]["parts"]) == 3
        assert json.loads(requests[-1]["body"]["messages"][1]["content"])["task"] == "plan"
        assert result["state"]["drafts"] == []
        for index in range(4):
            pending = result["state"]["pending"]
            job = client.post(
                f"/api/runs/{run_id}/decision",
                headers=HEADERS,
                json={
                    "interrupt_id": result["interrupt_id"],
                    "artifact_id": pending["artifact_id"],
                    "version": pending["version"],
                    "action": "approve",
                },
            ).json()
            completed_job = wait(client, job)
            assert completed_job["status"] == "completed", completed_job
            result = client.get("/api/runs/" + run_id).json()
            if index < 3:
                assert result["state"]["pending"]["artifact_id"] == f"section-{index + 1}"
        assert result["state"]["status"] == "completed"
        assert result["state"]["current_index"] == 3
        assert client.get("/api/runs/" + original_id).json() == original


def test_web_confirm_and_download_docx_without_key(tmp_path, compatible_server):
    url, requests, _ = compatible_server
    db = tmp_path / "state.sqlite"
    app = create_app(db, tmp_path / "missing.env", tmp_path / "uploads")
    with TestClient(app) as client:
        config(client, url)
        job = client.post(
            "/api/runs",
            headers=HEADERS,
            files={
                "assignment": (
                    "brief.txt",
                    (
                        "题目：导出验收\n部分：分析；结论\n字数上限：900\n"
                        "引用格式：APA 7\n网络图片最少：2"
                    ).encode(),
                )
            },
        ).json()
        assert wait(client, job)["status"] == "completed"
        run_id = job["run_id"]
        result = client.get("/api/runs/" + run_id).json()
        for _ in range(3):
            pending = result["state"]["pending"]
            job = client.post(
                f"/api/runs/{run_id}/decision",
                headers=HEADERS,
                json={
                    "interrupt_id": result["interrupt_id"],
                    "artifact_id": pending["artifact_id"],
                    "version": pending["version"],
                    "action": "approve",
                },
            ).json()
            assert wait(client, job)["status"] == "completed"
            result = client.get("/api/runs/" + run_id).json()
        assert result["delivery"]["ready"] and not result["delivery"]["confirmed"]
        assert client.get(f"/api/runs/{run_id}/export.docx").status_code == 400
        api_calls = len(requests)
        client.delete("/api/config", headers=HEADERS)
        result = client.post(
            f"/api/runs/{run_id}/complete",
            headers=HEADERS,
            json={"content_hash": result["delivery"]["content_hash"]},
        ).json()
        assert result["delivery"]["confirmed"]
        assert len(requests) == api_calls
    with TestClient(create_app(db, tmp_path / "missing.env", tmp_path / "uploads")) as client:
        response = client.get(f"/api/runs/{run_id}/export.docx")
        assert response.status_code == 200
        assert response.headers["Content-Type"].startswith("application/vnd.openxmlformats")
        assert "filename*=UTF-8''" in response.headers["Content-Disposition"]
        doc = Document(BytesIO(response.content))
        assert doc.paragraphs[0].text == "导出验收"
        assert len(doc.inline_shapes) == 2
        assert any(paragraph.text == "References" for paragraph in doc.paragraphs)
        assert len(requests) == api_calls

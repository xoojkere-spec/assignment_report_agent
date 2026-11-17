import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def configure_api(compatible_server, monkeypatch, tmp_path, isolated_test_environment):
    url, _, _ = compatible_server
    monkeypatch.setenv("OPENAI_BASE_URL", url)
    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-key")
    # CLI 使用独立进程；用测试专属启动钩子注入同一模拟网络，禁止测试意外联网。
    bootstrap = tmp_path / "bootstrap"
    bootstrap.mkdir()
    (bootstrap / "sitecustomize.py").write_text(
        "import os\n"
        "import sys\n"
        "try:\n"
        f"    sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
        "    from _test_isolation import install\n"
        "    install()\n"
        "    from test_research import research_network\n"
        "    from assignment_agent import research\n"
        "    research.PublicFetcher = research_network.__wrapped__()[2]\n"
        "    from functools import partial\n"
        "    from assignment_agent import service\n"
        "    service.open_agent = partial(service.open_agent, autonomous=False)\n"
        "except Exception:\n"
        # sitecustomize 异常默认会被 Python 忽略；任何初始化失败都禁止继续执行。
        "    sys.stderr.write('CLI 测试启动钩子初始化失败\\n')\n"
        "    os._exit(98)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(bootstrap) + os.pathsep + os.environ.get("PYTHONPATH", ""))


def invoke(db, *args, check=True):
    # 每次使用新进程，验证恢复不依赖上一进程的内存。
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "assignment_agent",
            "--db",
            str(db),
            "--env-file",
            str(db.parent / "unused.env"),
            *args,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
        env={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp936"},
    )


@pytest.mark.parametrize("failure", ["import", "install"])
def test_cli_bootstrap_failure_stops_before_body(tmp_path, failure):
    hook = tmp_path / "bootstrap" / "sitecustomize.py"
    # 修改夹具实际生成的钩子，验证 CLI 专用路径而不是另一份模拟实现。
    fault = (
        "import builtins, types\n"
        "_original_import = builtins.__import__\n"
        "def _faulty_import(name, *args, **kwargs):\n"
        "    if name == '_test_isolation':\n"
        f"        if {failure!r} == 'import':\n"
        "            raise ImportError('private-fault-detail')\n"
        "        def broken_install():\n"
        "            raise RuntimeError('private-fault-detail')\n"
        "        return types.SimpleNamespace(install=broken_install)\n"
        "    return _original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = _faulty_import\n"
    )
    hook.write_text(fault + hook.read_text(encoding="utf-8"), encoding="utf-8")
    marker = tmp_path / "body-executed.txt"
    result = subprocess.run(
        [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=20,
    )
    assert result.returncode == 98
    assert not marker.exists()
    assert "CLI 测试启动钩子初始化失败" in result.stderr
    assert "private-fault-detail" not in result.stderr


def test_cli_bootstrap_installs_network_guard():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.create_connection(('203.0.113.1', 443), timeout=1)",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=20,
    )
    assert result.returncode != 0
    assert "ExternalNetworkBlocked" in result.stderr
    assert "CLI 测试启动钩子初始化失败" not in result.stderr


def test_cli_restart_revise_and_complete(tmp_path):
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({"title": "测试", "sections": ["分析", "结论"], "word_limit": 500}),
        encoding="utf-8",
    )
    db = tmp_path / "state.sqlite"
    result = json.loads(invoke(db, "start", "--spec", str(spec), "--run-id", "cli").stdout)
    restored = json.loads(invoke(db, "resume", "cli").stdout)
    assert restored == result

    def act(result, command, *extra):
        pending = result["state"]["pending"]
        return json.loads(
            invoke(
                db,
                command,
                "cli",
                "--interrupt-id",
                result["interrupt_id"],
                "--artifact-id",
                pending["artifact_id"],
                "--version",
                str(pending["version"]),
                *extra,
            ).stdout
        )

    result = act(result, "revise", "--feedback", "补充例子")
    assert result["state"]["outline"]["version"] == 2
    for _ in range(3):
        result = act(result, "approve")
    assert result["state"]["status"] == "completed"
    assert json.loads(invoke(db, "status", "cli").stdout) == result


def test_cli_errors_have_nonzero_exit_code(tmp_path):
    result = invoke(tmp_path / "state.sqlite", "status", "missing", check=False)
    assert result.returncode == 2
    assert "任务不存在" in result.stderr


def test_cli_materials_clarification_and_restart(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("题目：材料导入\n部分：分析；结论", encoding="utf-8")
    db = tmp_path / "state.sqlite"
    result = json.loads(
        invoke(db, "start", "--assignment", str(brief), "--run-id", "materials").stdout
    )
    assert result["interrupt_payload"]["kind"] == "requirements"
    assert json.loads(invoke(db, "resume", "materials").stdout) == result
    spec = tmp_path / "spec.json"
    spec.write_text(
        json.dumps({"title": "材料导入", "sections": ["分析", "结论"], "word_limit": 500}),
        encoding="utf-8",
    )
    clarified = json.loads(
        invoke(
            db,
            "clarify",
            "materials",
            "--interrupt-id",
            result["interrupt_id"],
            "--spec",
            str(spec),
            "--note",
            "用户确认上限 500",
        ).stdout
    )
    assert clarified["state"]["outline"] is not None
    assert clarified["state"]["drafts"] == []
    assert json.loads(invoke(db, "status", "materials").stdout) == clarified


def test_cli_direct_material_import(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text(
        "题目：材料导入\n部分：分析；结论\n字数上限：500\n引用格式：APA 7\n网络图片最少：2",
        encoding="utf-8",
    )
    result = json.loads(
        invoke(tmp_path / "state.sqlite", "start", "--assignment", str(brief)).stdout
    )
    assert result["state"]["outline"] is not None
    assert len(result["state"]["analysis"]["facts"]) == 5

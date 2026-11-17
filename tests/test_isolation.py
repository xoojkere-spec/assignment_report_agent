"""验证测试保护的真实行为，避免保护只在主进程生效。"""

import json
import os
import socket
import subprocess
import sys

import httpx
import pytest
from _test_isolation import MODEL_VARIABLES, PROJECT_ROOT, ExternalNetworkBlocked

from assignment_agent.model_adapter import read_settings


def test_inherited_model_and_proxy_variables_are_removed():
    assert all(name not in os.environ for name in MODEL_VARIABLES)
    assert not any(
        name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"} for name in os.environ
    )


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_external_tcp_is_blocked_before_connecting(method):
    with socket.socket() as sock, pytest.raises(ExternalNetworkBlocked):
        getattr(sock, method)(("203.0.113.1", 443))


def test_external_dns_and_udp_are_blocked():
    with pytest.raises(ExternalNetworkBlocked):
        socket.getaddrinfo("external.example", 443)
    with socket.socket(type=socket.SOCK_DGRAM) as sock, pytest.raises(ExternalNetworkBlocked):
        sock.sendto(b"not-sent", ("203.0.113.1", 53))


def test_local_http_and_mock_transport_still_work(compatible_server):
    url, requests, _ = compatible_server
    with httpx.Client() as client:
        response = client.post(
            url + "/chat/completions",
            headers={"Authorization": "Bearer test-only"},
            json={"messages": [{"content": json.dumps({"task": "advisor", "question": "test"})}]},
        )
    assert response.status_code == 200 and len(requests) == 1
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        assert client.get("https://external.example/").status_code == 200


def test_project_dotenv_is_ignored_but_temporary_config_is_read(monkeypatch, tmp_path):
    from assignment_agent import model_adapter

    assert model_adapter.dotenv_values(PROJECT_ROOT / ".env") == {}
    assert model_adapter.dotenv_values(PROJECT_ROOT / ".env.local") == {}
    env = tmp_path / ".env"
    env.write_text(
        "OPENAI_BASE_URL=http://localhost:1234/v1\nMODEL_NAME=isolated\nOPENAI_API_KEY=test-only\n",
        encoding="utf-8",
    )
    assert read_settings(env).model == "isolated"
    monkeypatch.setenv("MODEL_NAME", "override")
    assert read_settings(env).model == "override"


def test_python_child_inherits_network_and_dotenv_guard():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; from assignment_agent.model_adapter import dotenv_values; "
            "assert dotenv_values('.env') == {}; "
            "socket.create_connection(('203.0.113.1', 443), timeout=1)",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=20,
    )
    assert result.returncode != 0
    assert "ExternalNetworkBlocked" in result.stderr
    assert "无法安装环境保护" not in result.stderr

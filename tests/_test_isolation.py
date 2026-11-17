"""测试专用环境保护；不参与应用运行，也不作为操作系统安全沙箱。"""

import ipaddress
import socket
from pathlib import Path

MODEL_VARIABLES = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "MODEL_NAME",
    "MODEL_TIMEOUT_SECONDS",
    "MODEL_MAX_INPUT_CHARS",
    "MODEL_MAX_TOKENS",
)
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
_installed = False


class ExternalNetworkBlocked(AssertionError):
    """在真正发起网络访问前中止，避免被 HTTP 重试逻辑吞掉。"""


def require_loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if isinstance(host, str) and host.lower().rstrip(".") == "localhost":
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ExternalNetworkBlocked("测试禁止解析或连接外部主机，请注入模拟传输") from None
    if not address.is_loopback:
        raise ExternalNetworkBlocked("测试仅允许本机回环连接，请注入模拟传输")


def install():
    """同时用于 pytest 主进程和继承启动钩子的 Python 子进程。"""
    global _installed
    if _installed:
        return

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex
    original_gethostbyaddr = socket.gethostbyaddr
    original_sendto = socket.socket.sendto

    def guard_address(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            require_loopback(address[0])

    def connect(sock, address):
        guard_address(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        guard_address(sock, address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        # None 用于本地监听地址查询，不触发外部域名解析。
        if host is not None:
            require_loopback(host)
        return original_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(host):
        require_loopback(host)
        return original_gethostbyname(host)

    def gethostbyname_ex(host):
        require_loopback(host)
        return original_gethostbyname_ex(host)

    def gethostbyaddr(host):
        require_loopback(host)
        return original_gethostbyaddr(host)

    def sendto(sock, data, *args):
        guard_address(sock, args[-1])
        return original_sendto(sock, data, *args)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.socket.sendto = sendto
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex
    socket.gethostbyaddr = gethostbyaddr

    from assignment_agent import model_adapter

    original_dotenv_values = model_adapter.dotenv_values

    def dotenv_values(dotenv_path=None, *args, **kwargs):
        path = Path(dotenv_path or ".env").resolve()
        # 保留临时目录中的配置测试，但不读取开发者项目目录中的真实凭据。
        if path.parent == PROJECT_ROOT and (path.name == ".env" or path.name.startswith(".env.")):
            return {}
        return original_dotenv_values(dotenv_path, *args, **kwargs)

    model_adapter.dotenv_values = dotenv_values
    _installed = True

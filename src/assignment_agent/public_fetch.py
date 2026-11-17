"""有请求预算的公开资料下载；与携带模型 Key 的客户端完全分离。"""

import ipaddress
import socket
import time
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx

from assignment_agent.research_types import ResearchError


def public_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if (
            len(value) > 4096
            or parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.port not in {None, 80, 443}
            or any(
                key.lower()
                in {"token", "api_key", "key", "password", "signature", "x-amz-signature"}
                for key, _ in parse_qsl(parts.query)
            )
        ):
            raise ValueError
        host = parts.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        return parts._replace(fragment="").geturl()
    except (ValueError, TypeError, AttributeError):
        raise ResearchError("资料网址无效，或不是允许访问的公开 HTTP(S) 地址") from None


class PublicFetcher:
    def __init__(
        self, *, transport=None, resolver=socket.getaddrinfo, budget=40, before_request=None
    ):
        self.transport = transport
        self.resolver = resolver
        self.remaining = budget
        self.before_request = before_request

    def fork(self, budget=40, *, before_request=None):
        """分开检索和下载预算，同时保留相同的地址检查及测试网络。"""
        return PublicFetcher(
            transport=self.transport,
            resolver=self.resolver,
            budget=budget,
            before_request=before_request,
        )

    def get(self, url, *, params=None, limit=2_000_000):
        started = time.monotonic()
        url = str(httpx.URL(public_url(url), params=params)) if params else public_url(url)
        for _ in range(5):
            if time.monotonic() - started > 40:
                raise ResearchError("公开资料请求超过总耗时上限，已停止该地址")
            if self.remaining <= 0:
                raise ResearchError("本轮公开资料请求预算已用完，可恢复检索或补充更具体的关键词")
            original = httpx.URL(public_url(url))
            if self.before_request:
                self.before_request(original.host)
            self.remaining -= 1
            try:
                records = self.resolver(
                    original.host,
                    original.port or (443 if original.scheme == "https" else 80),
                    type=socket.SOCK_STREAM,
                )
                addresses = list(dict.fromkeys(record[4][0] for record in records))
                if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                    raise ResearchError("资料域名解析到非公开地址，已阻止访问")
                # 固定已检查的 IP，同时保留原始 Host 与 TLS 主机名，防止二次解析绕过地址检查。
                pinned = original.copy_with(host=addresses[0])
                host = original.host
                if original.port:
                    host += f":{original.port}"
                with httpx.Client(
                    transport=self.transport, trust_env=False, follow_redirects=False, timeout=12
                ) as client:
                    with client.stream(
                        "GET",
                        pinned,
                        headers={"Host": host, "User-Agent": "AssignmentAgent/0.1 research"},
                        extensions={"sni_hostname": original.host},
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            url = public_url(
                                urljoin(str(original), response.headers.get("location", ""))
                            )
                            continue
                        if response.status_code != 200:
                            raise ResearchError(f"公开资料服务返回 HTTP {response.status_code}")
                        data = bytearray()
                        for chunk in response.iter_bytes():
                            if time.monotonic() - started > 40:
                                raise ResearchError("公开资料下载超过总耗时上限，已停止该地址")
                            data.extend(chunk)
                            if len(data) > limit:
                                raise ResearchError("公开资料超过下载大小限制")
                        return str(original), response.headers.get("content-type", ""), bytes(data)
            except ResearchError:
                raise
            except (OSError, ValueError, httpx.HTTPError):
                raise ResearchError("公开资料请求失败或超时，请稍后恢复检索") from None
        raise ResearchError("公开资料重定向次数过多")

    def json(self, url, params=None):
        import json

        _, _, raw = self.get(url, params=params)
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise ResearchError("公开检索服务未返回有效 JSON") from None

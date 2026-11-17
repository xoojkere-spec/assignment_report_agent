"""免费公开检索的后备切换；限流后冷却，不尝试绕过验证码。"""

import re
import time
from urllib.parse import urlsplit

from lxml import etree

from assignment_agent.public_fetch import public_url
from assignment_agent.research_budget import SearchBudgetReached
from assignment_agent.research_search import normalized
from assignment_agent.research_types import Paper, ResearchError
from assignment_agent.web_sources import web_candidates


class FreeWebSearch:
    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.cooldowns = {}
        self.events = []
        self.domain_cursor = 0

    def search(self, query, *, on_results=None):
        self.events = []
        failures = []
        found = {}
        attempted = set()
        broad = re.sub(r"(?:^|\s)-?site:\S+", " ", query).strip()
        variants = list(dict.fromkeys([query, broad]))
        if "learn.microsoft.com" in query:
            domains = ["techcommunity.microsoft.com", "devblogs.microsoft.com"]
            variants.append(f"site:{domains[self.domain_cursor % len(domains)]} {broad}")
            self.domain_cursor += 1
        for variant in variants:
            providers = ["duckduckgo", "bing"]
            if re.search(r"windows|microsoft|azure|\.net", variant, re.I) and self.matches_scope(
                variant, "https://learn.microsoft.com/"
            ):
                providers.append("microsoft-learn")
            for provider in providers:
                request_key = (provider, broad if provider == "microsoft-learn" else variant)
                if request_key in attempted:
                    continue
                attempted.add(request_key)
                if self.cooldowns.get(provider, 0) > time.time():
                    self.events.append({"engine": provider, "query": variant, "status": "cooldown"})
                    continue
                try:
                    candidates = self.lookup(provider, variant)
                    candidates = [
                        p
                        for p in candidates
                        if self.matches_scope(variant, p.url)
                        and self.relevant(variant, p.title, p.abstract)
                    ]
                    self.events.append(
                        {
                            "engine": provider,
                            "query": variant,
                            "status": "completed",
                            "results": len(candidates),
                            "urls": [p.url for p in candidates],
                        }
                    )
                    for paper in candidates:
                        found.setdefault(paper.url, paper)
                except SearchBudgetReached:
                    # 上一个渠道已返回的候选仍可用于核验。
                    if found:
                        return self.rank(found, broad)
                    raise
                except ResearchError as exc:
                    message = str(exc)
                    if any(code in message for code in ("HTTP 202", "HTTP 429", "验证码")):
                        self.cooldowns[provider] = time.time() + 300
                    self.events.append(
                        {"engine": provider, "query": variant, "status": "failed", "error": message}
                    )
                    failures.append(provider + "：" + message)
                else:
                    if candidates and on_results and on_results(candidates):
                        return self.rank(found, broad)
        if failures and not found:
            raise ResearchError("免费网页检索后备渠道未取得结果；" + "；".join(failures))
        return self.rank(found, broad)

    @staticmethod
    def rank(found, broad):
        return sorted(
            found.values(),
            key=lambda p: (
                -len(
                    set(normalized(broad).split())
                    & set(normalized(p.title + " " + p.abstract).split())
                )
            ),
        )[:12]

    @staticmethod
    def matches_scope(query, url):
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        includes = re.findall(r"(?<![-\w])site:([\w.-]+)", query, re.I)
        excludes = re.findall(r"-site:([\w.-]+)", query, re.I)

        def matches(domain):
            domain = domain.lower().rstrip(".")
            return host == domain or host.endswith("." + domain)

        return not any(matches(d) for d in excludes) and (
            not includes or any(matches(d) for d in includes)
        )

    @staticmethod
    def relevant(query, title, description):
        query = re.sub(r"(?:^|\s)-?site:\S+", " ", query)
        words = set(normalized(query).split()) - {"site", "com", "org", "en", "us", "https"}
        text = set(normalized(title + " " + description).split())
        return (
            bool(words)
            and len(words & text) >= min(2, len(words))
            and len(words & text) / len(words) >= 0.4
        )

    def lookup(self, provider, query):
        if provider == "duckduckgo":
            return web_candidates(self.fetcher, query)
        if provider == "bing":
            _, _, raw = self.fetcher.get(
                "https://www.bing.com/search", params={"q": query, "format": "rss"}
            )
            try:
                root = etree.fromstring(
                    raw, parser=etree.XMLParser(resolve_entities=False, no_network=True)
                )
            except (etree.XMLSyntaxError, ValueError):
                raise ResearchError("免费搜索后备未返回有效 RSS") from None
            entries = [
                (n.findtext("title", ""), n.findtext("link", ""), n.findtext("description", ""))
                for n in root.findall(".//item")
            ]
        else:
            data = self.fetcher.json(
                "https://learn.microsoft.com/api/search",
                {
                    "search": re.sub(r"(?:^|\s)-?site:\S+", " ", query).strip(),
                    "locale": "en-us",
                    "$top": 8,
                },
            )
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise ResearchError("官方文档搜索返回格式无效")
            entries = [
                (n.get("title", ""), n.get("url", ""), n.get("description", ""))
                for n in data["results"]
                if isinstance(n, dict)
            ]
        results = []
        seen = set()
        for title, url, description in entries:
            if not all(isinstance(value, str) for value in (title, url, description)):
                continue
            if not title or len(title) > 1000 or not self.relevant(query, title, description):
                continue
            try:
                url = public_url(url)
            except ResearchError:
                continue
            if url in seen:
                continue
            seen.add(url)
            results.append(
                Paper(
                    title=title,
                    url=url,
                    provider=provider,
                    abstract=description[:1600],
                    metadata_pending=True,
                )
            )
        return results[:5]

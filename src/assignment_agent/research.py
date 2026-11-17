"""可恢复的论文来源检索；所有副作用独立于写作确认图。"""

import hashlib
import json
from itertools import zip_longest
from urllib.parse import quote, urljoin, urlsplit

from lxml import html

from assignment_agent.image_policy import WEB_IMAGE_METHODS
from assignment_agent.paper_figures import EXTRACTOR_VERSION, inspect_paper, save_asset
from assignment_agent.public_fetch import PublicFetcher, public_url
from assignment_agent.research_budget import SearchBudgetReached
from assignment_agent.research_search import (
    LiteratureSearch,
    normalize_doi,
    normalized,
    plain,
    title_matches,
)
from assignment_agent.research_store import context_hash, timestamp
from assignment_agent.research_types import ResearchError
from assignment_agent.web_sources import (
    WEB_FIGURE_VERSION,
    WEB_TEXT_VERSION,
    IncompleteBody,
    article_metadata,
    documentation_figures,
    page_figures,
    page_text,
    resolve_web_paper,
)


def source_id(paper):
    # 标题与年份作为跨库合并键；DOI 与 PDF 哈希另用于核对和数量去重。
    key = normalized(paper.title) + "|" + str(paper.year)
    return "src-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def reconcile_pdf_authors(source, store):
    """仅当 DOI 作者逐一出现在论文首页时，纠正搜索索引留下的错误署名。"""
    metadata = source.get("citation_metadata") or {}
    people = metadata.get("authors") or []
    if len(people) != len(source.get("authors") or []) or not store.asset_valid(
        source.get("text_asset")
    ):
        return False
    try:
        path = store.assets / source["text_asset"]["filename"]
        pages = json.loads(path.read_text(encoding="utf-8"))
        first = " " + normalized(" ".join(page["text"] for page in pages[:2])) + " "
        names = []
        for person in people:
            names.append(" ".join(filter(None, (person.get("given"), person.get("family")))))
        if not all(name and " " + normalized(name) + " " in first for name in names):
            return False
    except (OSError, KeyError, TypeError, ValueError):
        return False
    if names == source["authors"]:
        return False
    source["authors"] = names
    source["paper"] = {
        **source["paper"],
        "authors": names,
        "family_names": [person["family"] for person in people],
    }
    return True


def recheck_known_url(fetcher, url, **kwargs):
    """已知来源偶发 403 时重取同一网址，仍按原规则核验返回内容。"""
    try:
        return fetcher.get(url, **kwargs)
    except ResearchError as exc:
        if str(exc) != "公开资料服务返回 HTTP 403":
            raise
        return fetcher.get(url, **kwargs)


def inspect_page(raw, url, paper):
    try:
        root = html.fromstring(raw)
    except (ValueError, html.etree.ParserError):
        raise ResearchError("来源网页不是可解析的 HTML") from None
    metadata = {}
    for item in root.xpath("//meta[@content]"):
        key = (item.get("name") or item.get("property") or "").lower()
        metadata.setdefault(key, []).append(item.get("content"))
    title = (
        metadata.get("citation_title")
        or metadata.get("dc.title")
        or metadata.get("og:title")
        or root.xpath("//title/text()")
        or [""]
    )[0]
    doi = normalize_doi((metadata.get("citation_doi") or [None])[0])
    text = " ".join(root.text_content().split())[:100_000]
    author_text = " ".join(metadata.get("citation_author", [])) + " " + text
    article_authors, article_dates = article_metadata(root, url)
    author_text += " " + " ".join(article_authors)
    if paper.provider == "microsoft-learn":
        verified = (
            urlsplit(url).hostname == "learn.microsoft.com"
            and title_matches(paper.title, title)
            and paper.authors == ["Microsoft"]
            and str(paper.year) in " ".join(metadata.get("ms.date", []))
        )
        return verified, page_text(raw)[:1800], []
    verified = (
        title_matches(paper.title, title)
        and bool(paper.authors)
        and any(
            normalized(author).split()[-1] in normalized(author_text).split()
            for author in paper.authors
            if normalized(author)
        )
        and paper.year is not None
        and str(paper.year)
        in " ".join(metadata.get("citation_publication_date", []) + article_dates) + " " + text
    )
    if paper.doi and doi and paper.doi != doi:
        verified = False
    pdfs = list(metadata.get("citation_pdf_url", []))
    pdfs += root.xpath("//a[@type='application/pdf']/@href | //link[@type='application/pdf']/@href")
    return verified, text[:1800], [urljoin(url, value) for value in pdfs[:3]]


class SearchFetchAdapter:
    """无重定向钩子的注入客户端按每次请求方法调用计数。"""

    def __init__(self, fetcher, reserve):
        self.fetcher = fetcher
        self.reserve = reserve

    def get(self, url, **kwargs):
        self.reserve(urlsplit(url).hostname)
        return self.fetcher.get(url, **kwargs)

    def json(self, url, params=None):
        self.reserve(urlsplit(url).hostname)
        return self.fetcher.json(url, params)


class ResearchCollector:
    def __init__(self, store, *, fetcher=None, search=None, budget=None):
        self.store = store
        self.fetcher = fetcher or PublicFetcher()
        self.budget = budget
        self.injected_search = search is not None
        if self.injected_search:
            self.search = search
        else:
            reserve = budget.reserve if budget else None
            search_fetcher = self.fetcher
            if hasattr(self.fetcher, "fork"):
                search_fetcher = self.fetcher.fork(before_request=reserve)
            elif reserve:
                search_fetcher = SearchFetchAdapter(self.fetcher, reserve)
            self.search = LiteratureSearch(
                search_fetcher, lookup_fetcher=self.fetcher if budget else None
            )

    def verify_doi(self, paper):
        if not paper.doi:
            return {}
        try:
            data = self.fetcher.json("https://api.crossref.org/works/" + quote(paper.doi, safe="/"))
        except ResearchError as exc:
            if "HTTP 404" not in str(exc):
                raise
            # arXiv、部分机构仓储等 DOI 由 DataCite 注册，Crossref 404 不等于 DOI 不存在。
            data = self.fetcher.json("https://api.datacite.org/dois/" + quote(paper.doi, safe="/"))
            try:
                entry = data["data"]["attributes"]
                if normalize_doi(entry["doi"]) != paper.doi or not any(
                    title_matches(paper.title, plain(item["title"])) for item in entry["titles"]
                ):
                    raise ResearchError("DOI 对应的文章标题与候选来源不一致")
                year = int(entry["publicationYear"])
                if paper.year and year != paper.year:
                    raise ResearchError("DOI 对应的发表年份与候选来源不一致")
            except (KeyError, TypeError, IndexError, AttributeError, ValueError) as error:
                if isinstance(error, ResearchError):
                    raise
                raise ResearchError("DataCite DOI 元数据不完整，尚不能核对该来源") from None
            return {}
        try:
            entry = data["message"]
            if normalize_doi(entry["DOI"]) != paper.doi or not title_matches(
                paper.title, entry["title"][0]
            ):
                raise ResearchError("DOI 对应的文章标题与候选来源不一致")
            year = (entry.get("published", {}).get("date-parts") or [[None]])[0][0]
            if paper.year and year and paper.year != year:
                raise ResearchError("DOI 对应的发表年份与候选来源不一致")
        except (KeyError, TypeError, IndexError, AttributeError):
            raise ResearchError("DOI 元数据不完整，尚不能核对该来源") from None
        return {
            "authors": [
                {"given": author.get("given", ""), "family": author.get("family", "")}
                for author in entry.get("author", [])
                if author.get("family")
            ],
            "journal": (entry.get("container-title") or [""])[0],
            "volume": entry.get("volume") or "",
            "issue": entry.get("issue") or "",
            "page": entry.get("page") or "",
            "article_number": entry.get("article-number") or "",
        }

    def inspect_source(self, run_id, digest, source, *, rechecking=False):
        from assignment_agent.research_types import Paper

        paper = Paper.model_validate(source["paper"])
        cached = source.get("pdf")
        if cached and self.store.asset_valid(cached):
            result = inspect_paper(
                (self.store.assets / cached["filename"]).read_bytes(), paper, self.store.assets
            )
            self.persist_pdf(run_id, digest, source, result, source["pdf_url"])
            return
        if (
            source.get("verified")
            and not paper.pdf_urls
            and source.get("web_text_version") == WEB_TEXT_VERSION
            and self.store.asset_valid(source.get("page_asset"))
            and self.store.asset_valid(source.get("text_asset"))
        ):
            # 仅升级网页提图时复用已核对的原网页，避免动态 HTML 改变已写章节的来源快照。
            raw = (self.store.assets / source["page_asset"]["filename"]).read_bytes()
            source["figure_count"] = self.persist_web_figures(
                run_id, digest, source, raw, source["url"]
            )
            self.store.save(
                run_id, digest, "source", {**source, "web_extractor_version": WEB_FIGURE_VERSION}
            )
            return
        if paper.doi and hasattr(self.search, "resolve_open_access"):
            try:
                open_access = self.search.resolve_open_access(paper)
                if open_access:
                    paper.pdf_urls = list(dict.fromkeys([*open_access.pdf_urls, *paper.pdf_urls]))[
                        :5
                    ]
                    paper.landing_urls = list(
                        dict.fromkeys([*open_access.landing_urls, *paper.landing_urls])
                    )[:4]
                    source["paper"] = paper.model_dump()
            except ResearchError:
                pass
        citation_metadata = self.verify_doi(paper)
        if citation_metadata:
            source["citation_metadata"] = citation_metadata
        urls = list(dict.fromkeys([*paper.pdf_urls[:3], *paper.landing_urls[:2], paper.url]))[:6]
        errors = []
        page_result = None
        seen_urls = set()
        for url in urls:
            try:
                if public_url(url) in seen_urls:
                    continue
                self.store.event(
                    run_id,
                    digest,
                    "source.fetch_started",
                    entry_id=source["id"],
                    url=public_url(url),
                )
                if rechecking:
                    final, content_type, raw = recheck_known_url(
                        self.fetcher, url, limit=20_000_000
                    )
                else:
                    final, content_type, raw = self.fetcher.get(url, limit=20_000_000)
                seen_urls.add(final)
                if raw.startswith(b"%PDF-"):
                    if paper.metadata_pending:
                        raise ResearchError(
                            "搜索摘要未提供 PDF 的可核对书目信息，继续寻找索引或原文网页"
                        )
                    result = inspect_paper(raw, paper, self.store.assets)
                    self.persist_pdf(run_id, digest, source, result, final)
                    return
                if "html" not in content_type and not raw.lstrip().lower().startswith(
                    (b"<!doctype html", b"<html")
                ):
                    raise ResearchError("图源既不是 PDF 也不是可核对的论文网页")
                if len(raw) > 2_000_000:
                    raise ResearchError("论文网页超过大小限制")
                if paper.metadata_pending:
                    paper = resolve_web_paper(raw, final, paper)
                    self.verify_doi(paper)
                    source.update(
                        title=paper.title,
                        authors=paper.authors,
                        year=paper.year,
                        doi=paper.doi,
                        paper=paper.model_dump(),
                    )
                    for link in paper.pdf_urls:
                        if link not in urls and len(urls) < 8:
                            urls.append(public_url(link))
                verified, evidence, links = inspect_page(raw, final, paper)
                for link in links:
                    if link not in urls and len(urls) < 8:
                        urls.append(public_url(link))
                if verified:
                    try:
                        body = page_text(raw, require_body=True)
                    except IncompleteBody:
                        self.store.event(run_id, digest, "source.body_retry", entry_id=source["id"])
                        final, _, raw = self.fetcher.get(final)
                        verified, evidence, _ = inspect_page(raw, final, paper)
                        if not verified:
                            raise ResearchError("正文重取后的来源元数据不对应") from None
                        body = page_text(raw, require_body=True)
                    page_result = {
                        "verified": True,
                        "verification": "网页标题、作者与年份对应；不代表正文论据已核验",
                        "url": final,
                        "evidence": evidence,
                        "web_text_version": WEB_TEXT_VERSION,
                        "body_chars": len(body),
                        "page_asset": save_asset(self.store.assets, raw, "html"),
                        "text_asset": save_asset(
                            self.store.assets,
                            json.dumps(
                                [
                                    {
                                        "page": None,
                                        "text": body,
                                    }
                                ],
                                ensure_ascii=False,
                            ).encode(),
                            "json",
                        ),
                    }
                    page_result["figure_count"] = self.persist_web_figures(
                        run_id, digest, {**source, **page_result}, raw, final
                    )
                    page_result["web_extractor_version"] = WEB_FIGURE_VERSION
            except ResearchError as exc:
                errors.append(str(exc))
                self.store.event(
                    run_id, digest, "source.fetch_failed", entry_id=source["id"], error=str(exc)
                )
        if page_result:
            self.store.save(
                run_id,
                digest,
                "source",
                {**source, **page_result, "error": "；".join(errors), "checked_at": timestamp()},
            )
        else:
            raise ResearchError(
                "；".join(dict.fromkeys(errors)) or "来源网页和 PDF 未通过对应性核对"
            )

    def persist_web_figures(self, run_id, digest, source, raw, url):
        def failure(image_url, error):
            self.store.event(
                run_id,
                digest,
                "figure.web_failed",
                entry_id=source["id"],
                url=image_url,
                error=error,
            )

        figures = []
        if source["paper"]["provider"] == "microsoft-learn":
            try:
                figures = [
                    {**f, "method": "licensed_web_image"}
                    for f in documentation_figures(raw, url, self.fetcher, self.store.assets)
                ]
            except ResearchError as exc:
                failure(url, str(exc))
        figures += page_figures(
            raw,
            url,
            self.fetcher,
            self.store.assets,
            skip_urls={f.get("display_url") for f in figures},
            on_error=failure,
        )
        active_ids = set()
        for figure in figures:
            identity = source["id"] + "|" + figure["sha256"]
            figure_id = "fig-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            if figure_id in active_ids:
                continue
            active_ids.add(figure_id)
            self.store.save(
                run_id,
                digest,
                "figure",
                {
                    **figure,
                    "id": figure_id,
                    "retired": False,
                    "source_id": source["id"],
                    "source_url": url,
                    "page_sha256": source["page_asset"]["sha256"],
                    "pdf_url": None,
                    "extracted_at": timestamp(),
                },
            )
        for old in self.store.entries(run_id, digest, "figure"):
            if old["source_id"] == source["id"] and old["id"] not in active_ids:
                self.store.save(run_id, digest, "figure", {**old, "retired": True})
        return len(active_ids)

    def persist_pdf(self, run_id, digest, source, result, pdf_url):
        from assignment_agent.research_types import Paper

        paper = Paper.model_validate(source["paper"])
        source.update(
            authors=paper.authors,
            paper=paper.model_dump(),
            verified=True,
            verification="PDF 标题、作者与年份对应；不代表正文论据已核验",
            pdf=result["pdf"],
            text_asset=result["text_asset"],
            pdf_url=public_url(pdf_url),
            evidence=result["evidence"],
            checks=result["checks"],
            license=result["license"],
            error=None,
            checked_at=timestamp(),
            figure_warnings=result["warnings"],
            extractor_version=result["extractor_version"],
            parsed_pages=result["parsed_pages"],
            total_pages=result["total_pages"],
            figure_count=len(result["figures"]),
        )
        reconcile_pdf_authors(source, self.store)
        self.store.save(run_id, digest, "source", source)
        for figure in result["figures"]:
            identity = source["id"] + "|" + figure["sha256"]
            value = {
                **figure,
                "id": "fig-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                "source_id": source["id"],
                "source_url": source["url"],
                "pdf_url": source["pdf_url"],
                "method": "pdf_page_crop",
                "extracted_at": timestamp(),
            }
            self.store.save(run_id, digest, "figure", value)
        self.store.event(
            run_id,
            digest,
            "source.pdf_verified",
            entry_id=source["id"],
            figures=len(result["figures"]),
            pdf_sha256=result["pdf"]["sha256"],
        )

    def refresh_source(self, run_id, digest, source):
        """按已知地址重验过期来源，保持搜索额度独立。"""
        key = source["id"]
        self.store.event(run_id, digest, "source.refresh_started", entry_id=key)
        try:
            self.inspect_source(run_id, digest, source, rechecking=True)
            fresh = next(
                item for item in self.store.entries(run_id, digest, "source") if item["id"] == key
            )
            self.store.save(
                run_id, digest, "source", {**fresh, "retired": False, "automatic_review": None}
            )
            self.store.event(run_id, digest, "source.refreshed", entry_id=key)
        except ResearchError as exc:
            self.store.save(
                run_id,
                digest,
                "source",
                {**source, "verified": False, "retired": True, "error": str(exc)},
            )
            self.store.event(run_id, digest, "source.refresh_failed", entry_id=key, error=str(exc))
            return f"《{source['title']}》重新取证失败：{exc}"
        return None

    def collect(
        self,
        run_id,
        state,
        queries,
        *,
        refresh=False,
        ranker=None,
        web_queries=None,
        model_candidates=None,
        figure_target=0,
        figure_queries=None,
        stop_when=None,
    ):
        digest = context_hash(state)
        old = self.store.run(run_id)
        old = old if old and old["context_hash"] == digest else {}
        automatic = state.automatic_research or state.model_profile.kind == "openai_compatible"
        pending = set((old.get("pending_pool") or {}).get("source", []))
        requested = list(queries)
        requested_academic = list(requested)
        queries = list(dict.fromkeys([*old.get("queries", []), *requested]))[-6:]
        web_queries = list(requested if web_queries is None else web_queries)
        requested_web = list(web_queries)
        requested += web_queries
        requested_figures = list(figure_queries or requested_academic) if figure_target else []
        requested += requested_figures
        web_queries = list(dict.fromkeys([*old.get("web_queries", []), *web_queries]))[-6:]
        # 网页查询和论文查询交错，避免候选名额先被宽泛学术结果占满。
        scheduled = list(
            dict.fromkeys(
                query
                for pair in zip_longest(requested_figures, requested_academic, requested_web)
                for query in pair
                if query is not None
            )
        )
        completed = list(old.get("completed_searches", []))
        chapter = state.current_index + 1 if stop_when else None
        if stop_when and old.get("discovery_chapter") != chapter:
            completed = []
        if refresh:
            if not stop_when:
                completed = [key for key in completed if key.split(":", 1)[-1] not in requested]
            for source in self.store.entries(run_id, digest, "source"):
                if (
                    not old.get("collecting")
                    and not (source["id"] in pending and not source.get("error"))
                    and not self.store.accepted(source)
                    and not self.reusable(source)
                ):
                    # 错误候选退场；已核对的全文留给下一次改写，不能因尚无正文采用而丢失。
                    self.store.save(run_id, digest, "source", {**source, "retired": True})
        self.store.save_run(
            run_id,
            digest,
            status="running",
            automatic=automatic,
            collecting=bool(self.budget and automatic),
            discovery_chapter=chapter,
            completed_searches=completed,
            queries=queries,
            web_queries=web_queries,
            figure_queries=requested_figures,
            issues=[],
            started_at=timestamp(),
        )
        issues = []
        if refresh:
            # 已用来源因网页更新等失效时，先走完整取证流程；不能只退休后永久丢失。
            anchors = set(self.store.passed_source_ids(run_id, digest))
            for source in self.store.entries(run_id, digest, "source"):
                if (
                    source["id"] not in anchors
                    or self.reusable(source)
                    or (source.get("review") or {}).get("action") == "reject"
                ):
                    continue
                if issue := self.refresh_source(run_id, digest, source):
                    issues.append(issue)
        web = getattr(self.search, "web", None)
        if web:
            web.cooldowns = old.get("web_cooldowns", {})
            web.domain_cursor = old.get("web_domain_cursor", 0)
        usage = self.budget.usage() if self.budget else None
        search_stopped = bool(usage and usage["exhausted"])
        processed = set()

        def process_candidates():
            sources = [
                item
                for item in self.store.entries(run_id, digest, "source")
                if not item.get("retired")
            ]
            if ranker and sources:
                figures = self.store.entries(run_id, digest, "figure")
                selection_candidates = [
                    {
                        **source,
                        "retrieval_needed": not self.ready(
                            source,
                            [
                                figure
                                for figure in figures
                                if figure["source_id"] == source["id"] and not figure.get("retired")
                            ],
                            need_figures=bool(figure_target),
                        ),
                        "figure_inventory": [
                            {
                                "caption": figure["caption"][:250],
                                "license": figure["license"],
                                "copyright_check": figure.get("image_review"),
                            }
                            for figure in figures
                            if figure["source_id"] == source["id"] and not figure.get("retired")
                        ][:8],
                    }
                    for source in sources
                ]
                if stop_when and not any(item["retrieval_needed"] for item in selection_candidates):
                    return
                chosen = ranker(selection_candidates)
                known = {item["id"] for item in sources}
                if not set(chosen).issubset(known):
                    raise ResearchError("模型筛选包含不存在的来源编号，未下载未核实网址")
                retained = [
                    item["id"] for item in selection_candidates if not item["retrieval_needed"]
                ]
                self.store.save_run(
                    run_id,
                    digest,
                    collecting=bool(stop_when and scheduled),
                    pending_pool={
                        "source": list(dict.fromkeys([*chosen, *retained])),
                        "figure": [],
                    },
                )
                self.store.event(
                    run_id,
                    digest,
                    "source.selection",
                    selected_ids=chosen,
                    retained_ids=retained,
                    figure_target=figure_target,
                )
                for source in sources:
                    source["selection_rank"] = (
                        chosen.index(source["id"]) if source["id"] in chosen else None
                    )
                    self.store.save(run_id, digest, "source", source)
                    if (
                        source["id"] not in chosen
                        and not self.store.accepted(source)
                        and not self.reusable(source)
                    ):
                        self.store.save(run_id, digest, "source", {**source, "retired": True})
                sources = [
                    item
                    for item in sources
                    if self.store.accepted(item) or self.reusable(item) or item["id"] in chosen
                ]
                sources.sort(
                    key=lambda item: chosen.index(item["id"]) if item["id"] in chosen else -1
                )
            elif self.budget and automatic:
                self.store.save_run(
                    run_id,
                    digest,
                    collecting=bool(stop_when and scheduled),
                    pending_pool={"source": [item["id"] for item in sources], "figure": []},
                )
            sources.sort(key=lambda item: not bool(item["paper"]["pdf_urls"]))
            for source in sources:
                figures = [
                    item
                    for item in self.store.entries(run_id, digest, "figure")
                    if item["source_id"] == source["id"] and not item.get("retired")
                ]
                if self.ready(source, figures, need_figures=bool(figure_target)):
                    continue
                paper = source["paper"]
                key = (source["id"], tuple(paper["pdf_urls"]), tuple(paper.get("landing_urls", [])))
                if key in processed:
                    continue
                if len(processed) >= 16:
                    break
                processed.add(key)
                try:
                    self.inspect_source(run_id, digest, source)
                except ResearchError as exc:
                    self.store.save(
                        run_id,
                        digest,
                        "source",
                        {**source, "verified": False, "error": str(exc), "checked_at": timestamp()},
                    )
                    issues.append(f"《{source['title']}》：{exc}")
                if stop_when and stop_when():
                    break

        def accept_candidates(candidates, query, provider):
            changed = self.save_candidates(run_id, digest, candidates, query, provider, ranker)
            if stop_when and changed:
                process_candidates()
                return stop_when()
            return False

        try:
            for hint in model_candidates or []:
                try:
                    url = public_url(hint.url)
                except ResearchError:
                    issues.append("模型候选网址不符合公开地址要求，已排除")
                    continue
                existing = self.store.entries(run_id, digest, "source")
                from assignment_agent.research_types import Paper

                paper = Paper(
                    title=hint.title, url=url, provider="model_candidate", metadata_pending=True
                )
                duplicate = next(
                    (
                        item
                        for item in existing
                        if item["url"] == url or item["id"] == source_id(paper)
                    ),
                    None,
                )
                if duplicate:
                    self.store.save(run_id, digest, "source", {**duplicate, "retired": False})
                    continue
                if sum(not item.get("retired") for item in existing) >= 60:
                    break
                self.store.save(
                    run_id,
                    digest,
                    "source",
                    {
                        "id": source_id(paper),
                        "paper": paper.model_dump(),
                        "title": paper.title,
                        "authors": [],
                        "year": None,
                        "doi": None,
                        "url": url,
                        "query": "",
                        "provider": "model_candidate",
                        "verified": False,
                        "review": None,
                        "retired": False,
                    },
                )
                self.store.event(
                    run_id,
                    digest,
                    "discovery.model_candidate",
                    entry_id=source_id(paper),
                    verified=False,
                )
            if stop_when and not stop_when():
                process_candidates()
            search_stopped = search_stopped or bool(stop_when and stop_when())
            providers = list(self.search.providers)
            if figure_target and getattr(self.search, "figure_provider", None):
                # 许可线索检索先于一般候选，避免候选预算全被不能转载的开放全文占满。
                providers.insert(0, self.search.figure_provider)
            for query in scheduled:
                if search_stopped:
                    break
                for provider in providers:
                    allowed = (
                        requested_figures
                        if provider == getattr(self.search, "figure_provider", None)
                        else requested_web
                        if provider == "web"
                        else requested_academic
                    )
                    if query not in allowed:
                        continue
                    if provider in getattr(self.search, "disabled", set()):
                        continue
                    key = provider + ":" + query
                    if key in completed:
                        continue
                    try:
                        if self.budget and self.injected_search:
                            self.budget.reserve(provider)
                        progressive_web = bool(
                            stop_when and web and provider == "web" and not self.injected_search
                        )
                        if progressive_web:
                            candidates = self.search.search(
                                provider,
                                query,
                                on_results=lambda batch, term=query: accept_candidates(
                                    batch, term, "web"
                                ),
                            )
                        else:
                            candidates = self.search.search(provider, query)
                        if not progressive_web:
                            accept_candidates(candidates, query, provider)
                        if not (progressive_web and stop_when()):
                            completed.append(key)
                        self.store.event(
                            run_id,
                            digest,
                            "search.completed",
                            provider=provider,
                            query=query,
                            results=len(candidates),
                        )
                    except SearchBudgetReached:
                        search_stopped = True
                    except ResearchError as exc:
                        issues.append(f"{provider}：{exc}")
                        self.store.event(
                            run_id,
                            digest,
                            "search.failed",
                            provider=provider,
                            query=query,
                            error=str(exc),
                        )
                    if provider == "web" and web:
                        for event in web.events:
                            self.store.event(run_id, digest, "search.free_engine", **event)
                        self.store.save_run(
                            run_id,
                            digest,
                            web_cooldowns=web.cooldowns,
                            web_domain_cursor=web.domain_cursor,
                        )
                    self.store.save_run(run_id, digest, completed_searches=completed)
                    if search_stopped or (stop_when and stop_when()):
                        search_stopped = True
                        break
            if not stop_when:
                process_candidates()
            summary = self.store.summary(run_id, state)
            verified = [
                item for item in summary["sources"] if item.get("verified") and item["integrity_ok"]
            ]
            if len(verified) < state.spec.min_sources:
                issues.append(
                    f"已核对来源 {len(verified)}/{state.spec.min_sources}；"
                    "可恢复检索或补充更具体的关键词"
                )
            if len(summary["figures"]) < summary["required_web_images"]:
                issues.append(
                    f"已提取论文图片 {len(summary['figures'])}/{summary['required_web_images']}；"
                    "尚未满足图片目标"
                )
            usage = self.budget.usage() if self.budget else None
            if issues and usage and usage["exhausted"]:
                issues.insert(0, self.budget.notice(usage))
            self.store.save_run(
                run_id,
                digest,
                status="needs_review" if not issues else "partial",
                collecting=False,
                issues=list(dict.fromkeys(issues))[:30],
                completed_searches=completed,
                finished_at=timestamp(),
            )
        except Exception:
            self.store.save_run(
                run_id,
                digest,
                status="interrupted",
                issues=["检索已中断，已保存来源保留，可恢复检索"],
            )
            raise
        return self.store.summary(run_id, state)

    def save_candidates(self, run_id, digest, candidates, query, provider, ranker):
        changed = False
        for paper in candidates:
            paper.url = public_url(paper.url)
            paper.pdf_urls = [public_url(url) for url in paper.pdf_urls[:5]]
            paper.landing_urls = [public_url(url) for url in paper.landing_urls[:4]]
            identity = source_id(paper)
            existing = next(
                (
                    item
                    for item in self.store.entries(run_id, digest, "source")
                    if item["id"] == identity or (paper.doi and item.get("doi") == paper.doi)
                ),
                None,
            )
            if existing:
                # 已核对的网页来源仍可补充全文链接，供恢复时继续提取论文图片。
                previous = {**existing["paper"]}
                previous["pdf_urls"] = list(dict.fromkeys([*previous["pdf_urls"], *paper.pdf_urls]))
                previous["landing_urls"] = list(
                    dict.fromkeys([*previous.get("landing_urls", []), *paper.landing_urls])
                )
                previous["abstract"] = previous.get("abstract") or paper.abstract
                hints = previous.get("license_hints", []) + paper.license_hints
                previous["license_hints"] = [
                    hint for index, hint in enumerate(hints) if hint not in hints[:index]
                ][:10]
                if previous["authors"] == paper.authors and paper.family_names:
                    previous["family_names"] = paper.family_names
                if previous != existing["paper"] or existing.get("retired"):
                    self.store.save(
                        run_id,
                        digest,
                        "source",
                        {**existing, "paper": previous, "retired": False},
                    )
                    changed = True
                continue
            if sum(
                not item.get("retired") for item in self.store.entries(run_id, digest, "source")
            ) >= (60 if ranker else 24):
                break
            self.store.save(
                run_id,
                digest,
                "source",
                {
                    "id": identity,
                    "paper": paper.model_dump(),
                    "title": paper.title,
                    "authors": paper.authors,
                    "year": paper.year,
                    "doi": paper.doi,
                    "url": paper.url,
                    "query": query,
                    "provider": provider,
                    "verified": False,
                    "review": None,
                },
            )
            changed = True
        return changed

    def ready(self, source, figures, *, need_figures=False):
        """已完整处理的来源自动复用，不占新增下载的筛选名额。"""
        return (
            source.get("verified")
            and self.store.asset_valid(source.get("pdf") or source.get("page_asset"))
            and self.store.asset_valid(source.get("text_asset"))
            and (source.get("pdf") or not source["paper"]["pdf_urls"])
            and (source.get("pdf") or source.get("web_text_version") == WEB_TEXT_VERSION)
            and (not source.get("pdf") or source.get("extractor_version") == EXTRACTOR_VERSION)
            and (not need_figures or len(figures) >= source.get("figure_count", 1))
            and all(
                self.store.asset_valid(item)
                and (
                    item.get("method") not in WEB_IMAGE_METHODS
                    or self.store.asset_valid(item.get("provenance_asset"))
                )
                for item in figures
            )
            and (source.get("pdf") or source.get("web_extractor_version") == WEB_FIGURE_VERSION)
        )

    def reusable(self, source):
        return (
            source.get("verified")
            and self.store.asset_valid(source.get("pdf") or source.get("page_asset"))
            and self.store.asset_valid(source.get("text_asset"))
            and (source.get("review") or {}).get("action") != "reject"
        )

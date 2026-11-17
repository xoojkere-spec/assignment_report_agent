"""公开网页检索及官方文档图源；网页、仓库原文、图片和许可必须互相对应。"""

import hashlib
import json
import re
from datetime import date
from io import BytesIO
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from lxml import html
from PIL import Image

from assignment_agent.paper_figures import save_asset
from assignment_agent.public_fetch import public_url
from assignment_agent.research_search import title_matches
from assignment_agent.research_types import Paper, ResearchError


def parse_html(raw):
    try:
        return html.fromstring(raw)
    except (ValueError, html.etree.ParserError):
        raise ResearchError("公开网页未返回可解析的 HTML") from None


def web_candidates(fetcher, query):
    _, _, raw = fetcher.get("https://html.duckduckgo.com/html/", params={"q": query})
    root = parse_html(raw)
    if root.xpath('//form[contains(@action, "anomaly")]'):
        raise ResearchError("公开网页搜索要求验证码，本轮不能取得检索结果")
    result = []
    seen = set()
    for link in root.xpath('//a[contains(concat(" ", @class, " "), " result__a ")]'):
        target = urljoin("https://duckduckgo.com/", link.get("href", ""))
        if urlsplit(target).hostname in {"duckduckgo.com", "www.duckduckgo.com"}:
            target = parse_qs(urlsplit(target).query).get("uddg", [""])[0]
        try:
            target = public_url(target)
        except ResearchError:
            continue
        if target in seen:
            continue
        seen.add(target)
        title = " ".join(link.text_content().split())
        container = link.xpath('ancestor::div[contains(@class,"result__body")][1]')
        snippets = (
            container[0].xpath('.//*[contains(@class,"result__snippet")]') if container else []
        )
        snippet = " ".join(snippets[0].text_content().split())[:1600] if snippets else ""
        if title and len(title) <= 1000:
            result.append(
                Paper(
                    title=title, url=target, provider="web", abstract=snippet, metadata_pending=True
                )
            )
        if len(result) == 5:
            break
    return result


def metadata(root):
    values = {}
    for node in root.xpath("//meta[@content]"):
        key = (node.get("name") or node.get("property") or "").lower()
        values.setdefault(key, []).append(node.get("content"))
    return values


def lwn_byline(root, url):
    """LWN 旧文章把署名和发表日期放在专用栏，不能用页脚或评论日期替代。"""
    address = urlsplit(url or "")
    if address.hostname not in {"lwn.net", "www.lwn.net"} or not re.fullmatch(
        r"/Articles/\d+/?", address.path
    ):
        return [], []
    bylines = root.xpath(
        '//*[contains(concat(" ", normalize-space(@class), " "), " FeatureByline ")]'
    )
    if len(bylines) != 1:
        return [], []
    byline = bylines[0]
    names = byline.xpath("./b")
    if len(names) != 1 or (byline.text or "").strip() != "By":
        return [], []
    author = " ".join(names[0].text_content().split())
    published = " ".join(byline.xpath("./br/following-sibling::text()")).strip()
    months = (
        "January February March April May June July August September October November December"
    ).split()
    match = re.fullmatch(r"(" + "|".join(months) + r") (\d{1,2}), ((?:19|20)\d{2})", published)
    if not author or len(author) > 200 or not match:
        return [], []
    try:
        value = date(int(match[3]), months.index(match[1]) + 1, int(match[2]))
    except ValueError:
        return [], []
    return [author], [value.isoformat()]


def article_metadata(root, url=None):
    """读取标准文章元数据及已识别站点的明确署名，不用修改日期推断发表日期。"""
    for script in root.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text or "")
        except (ValueError, TypeError):
            continue
        records = data if isinstance(data, list) else [data]
        for record in records:
            if not isinstance(record, dict):
                continue
            records += record.get("@graph", []) if isinstance(record.get("@graph"), list) else []
            kinds = record.get("@type", [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if not isinstance(kinds, list) or not set(kinds) & {
                "Article",
                "BlogPosting",
                "ScholarlyArticle",
                "NewsArticle",
            }:
                continue
            names = record.get("author", [])
            names = names if isinstance(names, list) else [names]
            names = [a.get("name") if isinstance(a, dict) else a for a in names]
            names = [a.strip() for a in names if isinstance(a, str) and a.strip()]
            date = record.get("datePublished")
            if names and isinstance(date, str):
                return names, [date]
    return lwn_byline(root, url)


def resolve_web_paper(raw, url, candidate):
    """搜索摘要不是书目证据，只接收原站可核对的论文元数据或微软官方文档元数据。"""
    root = parse_html(raw)
    values = metadata(root)
    title = (
        values.get("citation_title")
        or values.get("og:title")
        or root.xpath("//h1/text()")
        or root.xpath("//title/text()")
        or [""]
    )[0]
    if not (title_matches(candidate.title, title) or title_matches(title, candidate.title)):
        raise ResearchError("搜索结果标题与来源页面不对应")
    if not title or len(title) > 1000:
        raise ResearchError("来源网页标题缺失或超过大小限制")
    authors = values.get("citation_author", [])
    dates = values.get("citation_publication_date") or values.get("dc.date") or []
    if not authors or not dates:
        article_authors, article_dates = article_metadata(root, url)
        authors = authors or article_authors
        dates = dates or article_dates
    provider = "web"
    if urlsplit(url).hostname == "learn.microsoft.com":
        authors = ["Microsoft"]
        dates = values.get("ms.date", [])
        provider = "microsoft-learn"
    match = re.search(r"\b(?:19|20)\d{2}\b", " ".join(dates))
    if not authors or not match:
        raise ResearchError("来源网页没有可核对的作者及发表日期，不能只凭搜索摘要引用")
    from assignment_agent.research_search import normalize_doi

    return Paper(
        title=title,
        authors=authors,
        year=int(match[0]),
        doi=normalize_doi((values.get("citation_doi") or [None])[0]),
        url=url,
        pdf_urls=[urljoin(url, value) for value in values.get("citation_pdf_url", [])[:3]],
        provider=provider,
    )


WEB_TEXT_VERSION = 2
WEB_FIGURE_VERSION = 2


class IncompleteBody(ResearchError):
    """元数据有效但没有取得可用正文，不能视为全文证据。"""


def page_text(raw, *, require_body=False):
    root = parse_html(raw)
    candidates = []
    # 优先文章主体，避免选中页面顶部空占位容器或导航。
    for selector in ("//article", "//main", "//body"):
        texts = []
        for node in root.xpath(selector):
            text = " ".join(
                " ".join(
                    node.xpath(
                        ".//text()[not(ancestor::script) and not(ancestor::style) "
                        "and not(ancestor::nav) and not(ancestor::button) "
                        "and not(ancestor::footer)]"
                    )
                ).split()
            )
            texts.append(text)
        if texts:
            best = max(texts, key=len)
            candidates.append(best)
            if len(best) >= 80:
                break
    text = (
        max(candidates, key=len)
        if candidates
        else " ".join(
            " ".join(
                root.xpath(".//text()[not(ancestor::script) and not(ancestor::style)]")
            ).split()
        )
    )[:100_000]
    blocked = re.search(
        r"enable javascript|please click here if you are not redirected|"
        r"complete this challenge|^loading[.\s]*$|^captcha",
        text,
        re.I,
    )
    challenge_title = re.search(
        r"captcha|access denied|just a moment", " ".join(root.xpath("//title/text()")), re.I
    )
    if require_body and (len(text) < 80 or challenge_title or (blocked and len(text) < 500)):
        raise IncompleteBody("网页正文为空、过短或仅有加载/验证占位内容，不能作为全文证据")
    return text


def documentation_figures(raw, url, fetcher, assets):
    """仅自动使用可验证的官方仓库图片；没有完整的授权链就返回空集。"""
    if urlsplit(url).hostname != "learn.microsoft.com":
        return []
    root = parse_html(raw)
    images = root.xpath("//main//img[@src][@alt]")
    if not images:
        return []
    values = metadata(root)
    git_url = (values.get("github_feedback_content_git_url") or [""])[0]
    parts = urlsplit(git_url)
    match = re.fullmatch(r"/MicrosoftDocs/([\w.-]+)/blob/([\w.-]+)/(.+\.md)", parts.path)
    if parts.scheme != "https" or parts.hostname != "github.com" or not match:
        return []
    repository, branch, path = match.groups()
    if ".." in PurePosixPath(unquote(path)).parts:
        return []
    base = f"https://raw.githubusercontent.com/MicrosoftDocs/{repository}/{branch}/"
    source_url = base + path
    _, _, markdown = fetcher.get(source_url)
    _, _, license_raw = fetcher.get(base + "LICENSE")
    license_text = license_raw.decode("utf-8", errors="replace")
    if not license_text.lstrip().startswith("Attribution 4.0 International"):
        return []
    text = markdown.decode("utf-8", errors="replace")
    proof = {
        "method": "official_repository_license",
        "source_url": source_url,
        "source_sha256": hashlib.sha256(markdown).hexdigest(),
        "license_source_url": base + "LICENSE",
        "license_sha256": hashlib.sha256(license_raw).hexdigest(),
        "license_text": license_text,
    }
    proof_asset = save_asset(assets, json.dumps(proof, ensure_ascii=False).encode(), "json")
    figures = []
    for node in images:
        relative = node.get("src")
        caption = " ".join(node.get("alt", "").split())
        location = urlsplit(relative)
        if location.scheme or location.netloc or not caption or len(caption) < 12:
            continue
        # 仓库原文必须逐字关联同一图片，不能用仓库许可覆盖第三方外链或装饰图标。
        if not re.search(r"!\[[^\]]*\]\(" + re.escape(relative) + r"\)", text):
            continue
        if re.search(r"third.party|all rights reserved|used with permission", caption, re.I):
            continue
        image_url = public_url(urljoin(source_url, relative))
        display_url = public_url(urljoin(url, relative))
        _, _, original = fetcher.get(image_url, limit=8_000_000)
        _, _, displayed = fetcher.get(display_url, limit=8_000_000)
        if original != displayed:
            continue
        try:
            with Image.open(BytesIO(original)) as image:
                if (
                    image.width < 240
                    or image.height < 180
                    or image.width * image.height > 20_000_000
                ):
                    continue
                buffer = BytesIO()
                image.convert("RGB").save(buffer, "PNG")
                size = {"width": image.width, "height": image.height}
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
        figures.append(
            {
                **save_asset(assets, buffer.getvalue(), "png"),
                **size,
                "page": None,
                "figure_number": str(len(figures) + 1),
                "caption": caption,
                "image_url": image_url,
                "display_url": display_url,
                "original_sha256": hashlib.sha256(original).hexdigest(),
                "provenance_asset": proof_asset,
                "license": {
                    "status": "declared",
                    "url": "https://creativecommons.org/licenses/by/4.0/",
                    "evidence": (
                        "Official page links to its MicrosoftDocs source repository; "
                        "the exact image "
                        "is referenced in that Markdown and byte-identical to the page image. "
                        "Repository LICENSE: " + base + "LICENSE\n" + license_text[:2400]
                    ),
                    "scope": "official_repository_image",
                    "source_url": base + "LICENSE",
                },
            }
        )
        if len(figures) == 3:
            break
    return figures


def page_image_links(raw, url):
    """只关联文章正文实际引用的位图地址与原始图注，不把搜索缩略图当图源。"""
    root = parse_html(raw)
    nodes = root.xpath("//article//img | //main//img | //*[@id='mw-content-text']//img")
    if not nodes:
        nodes = root.xpath("//body//figure//img")
    found = {}
    for node in nodes:
        containers = node.xpath(
            "ancestor::figure[1] | ancestor::*[contains(@class,'thumbinner')][1]"
        )
        captions = (
            containers[0].xpath(
                ".//figcaption//text() | .//*[contains(@class,'thumbcaption')]//text()"
            )
            if containers
            else []
        )
        caption = " ".join(" ".join(captions).split()) or " ".join((node.get("alt") or "").split())
        if len(caption) < 12:
            continue
        try:
            target = public_url(urljoin(url, node.get("src") or ""))
        except ResearchError:
            continue
        if target != url:
            found.setdefault(target, caption[:600])
    return found


def page_figures(raw, url, fetcher, assets, *, skip_urls=(), on_error=None):
    """保存已核对文章中的图片与关联证据；没有图片专属授权时明确记录未知。"""
    figures = []
    for image_url, caption in list(page_image_links(raw, url).items())[:8]:
        if image_url in skip_urls:
            continue
        try:
            final, _, original = fetcher.get(image_url, limit=8_000_000)
            with Image.open(BytesIO(original)) as image:
                if (
                    image.width < 240
                    or image.height < 180
                    or image.width * image.height > 20_000_000
                ):
                    continue
                buffer = BytesIO()
                image.convert("RGB").save(buffer, "PNG")
                size = {"width": image.width, "height": image.height}
            original_hash = hashlib.sha256(original).hexdigest()
            proof = {
                "method": "verified_article_image",
                "page_url": url,
                "page_sha256": hashlib.sha256(raw).hexdigest(),
                "image_url": image_url,
                "resolved_image_url": final,
                "original_sha256": original_hash,
                "caption": caption,
            }
            figures.append(
                {
                    **save_asset(assets, buffer.getvalue(), "png"),
                    **size,
                    "page": None,
                    "figure_number": str(len(figures) + 1),
                    "caption": caption,
                    "image_url": image_url,
                    "display_url": image_url,
                    "original_sha256": original_hash,
                    "provenance_asset": save_asset(
                        assets, json.dumps(proof, ensure_ascii=False).encode(), "json"
                    ),
                    "license": {
                        "status": "unknown",
                        "url": None,
                        "evidence": "原网页引用该图片；未取得图片专属授权声明，"
                        "不推定网页通用许可适用于图片。",
                    },
                    "method": "web_page_image",
                }
            )
        except (ResearchError, OSError, ValueError, Image.DecompressionBombError) as exc:
            if on_error:
                on_error(
                    image_url,
                    str(exc)
                    if isinstance(exc, ResearchError)
                    else "图片无法解码或超出限制，已跳过",
                )
        if len(figures) >= 4:
            break
    return figures

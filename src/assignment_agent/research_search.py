"""将审计项目 ScholarSearch 的多库聚合思路适配为受限公开 HTTP 客户端。"""

import re
import unicodedata
from html import unescape

from assignment_agent.research_types import Paper, ResearchError


def normalized(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", value).casefold()))


def plain(value):
    return " ".join(re.sub(r"<[^>]+>", " ", unescape(unescape(value or ""))).split())


def openalex_paper(item):
    if item.get("is_retracted") or item.get("type") in {
        "dataset",
        "paratext",
        "supplementary-material",
    }:
        return None
    locations = list(item.get("locations") or [])
    locations.sort(
        key=lambda loc: (
            not bool(loc.get("pdf_url")),
            (loc.get("source") or {}).get("type") != "repository",
        )
    )
    pdfs = list(dict.fromkeys(loc["pdf_url"] for loc in locations if loc.get("pdf_url")))
    landings = list(
        dict.fromkeys(
            loc["landing_page_url"]
            for loc in locations
            if loc.get("is_oa") and loc.get("landing_page_url")
        )
    )
    positions = [
        (position, word)
        for word, indexes in (item.get("abstract_inverted_index") or {}).items()
        for position in indexes
    ]
    doi = normalize_doi(item.get("doi"))
    return Paper(
        title=plain(item.get("title")),
        authors=[a["author"]["display_name"] for a in item.get("authorships", [])],
        year=item.get("publication_year"),
        doi=doi,
        url=("https://doi.org/" + doi) if doi else (landings or [item["id"]])[0],
        pdf_urls=pdfs[:5],
        landing_urls=landings[:4],
        provider="openalex",
        abstract=" ".join(word for _, word in sorted(positions))[:12000],
        is_open_access=bool((item.get("open_access") or {}).get("is_oa")),
        license_hints=[
            {
                "license": loc.get("license"),
                "pdf_url": loc.get("pdf_url"),
                "landing_url": loc.get("landing_page_url"),
            }
            for loc in locations
            if loc.get("license")
        ][:10],
    )


def normalize_doi(value):
    if not isinstance(value, str) or not value:
        return None
    value = re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value.strip(), flags=re.I
    ).lower()
    return value if re.fullmatch(r"10\.\d{4,9}/[^\s?#]+", value) else None


def title_matches(title, text):
    words = set(normalized(title).split())
    return bool(words) and len(words & set(normalized(text).split())) / len(words) >= 0.85


class LiteratureSearch:
    providers = ("openalex", "crossref", "semanticscholar", "web")
    figure_provider = "openalex-figures"

    def __init__(self, fetcher, *, lookup_fetcher=None):
        from assignment_agent.free_search import FreeWebSearch

        self.fetcher = fetcher
        self.lookup_fetcher = lookup_fetcher or fetcher
        self.disabled = set()
        self.web = FreeWebSearch(fetcher)

    def resolve_open_access(self, paper):
        data = self.lookup_fetcher.json(
            "https://api.openalex.org/works/https://doi.org/" + paper.doi
        )
        result = openalex_paper(data)
        if result and title_matches(paper.title, result.title) and result.year == paper.year:
            return result
        return None

    def search(self, provider, query, *, on_results=None):
        if provider == "web":
            return self.web.search(query, on_results=on_results)
        if provider in {"openalex", self.figure_provider}:
            filters = "is_oa:true,is_retracted:false"
            if provider == self.figure_provider:
                filters += ",locations.license:cc-by|cc-by-sa|cc0"
            data = self.fetcher.json(
                "https://api.openalex.org/works",
                {"search": query, "filter": filters, "per_page": 8},
            )
            try:
                return [
                    paper for item in data["results"] if (paper := openalex_paper(item)) is not None
                ]
            except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                raise ResearchError("OpenAlex 检索结果结构无效") from None
        if provider == "crossref":
            data = self.fetcher.json(
                "https://api.crossref.org/works", {"query.bibliographic": query, "rows": 6}
            )
            try:
                items = data["message"]["items"]
                return [
                    Paper(
                        title=plain(item["title"][0]),
                        authors=[
                            " ".join(filter(None, [a.get("given"), a.get("family")]))
                            for a in item.get("author", [])
                        ],
                        family_names=[
                            family
                            if (family := re.sub(r"^[\s–—-]+|[\s–—-]+$", "", a.get("family") or ""))
                            else None
                            for a in item.get("author", [])
                        ],
                        year=(item.get("published", {}).get("date-parts") or [[None]])[0][0],
                        doi=normalize_doi(item.get("DOI")),
                        url=item["URL"],
                        pdf_urls=[
                            link["URL"]
                            for link in item.get("link", [])
                            if link.get("content-type") == "application/pdf"
                        ],
                        provider=provider,
                        abstract=plain(item.get("abstract", "")),
                    )
                    for item in items
                    if item.get("title")
                    and item.get("URL")
                    and item.get("type") not in {"component", "dataset", "reference-entry"}
                ][:6]
            except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                raise ResearchError("Crossref 检索结果结构无效") from None
        try:
            data = self.fetcher.json(
                "https://api.semanticscholar.org/graph/v1/paper/search",
                {
                    "query": query,
                    "limit": 6,
                    "fields": "title,authors,year,url,openAccessPdf,externalIds,abstract",
                },
            )
        except ResearchError as exc:
            if "HTTP 429" in str(exc):
                self.disabled.add(provider)
            raise
        try:
            return [
                Paper(
                    title=plain(item["title"]),
                    authors=[a["name"] for a in item.get("authors", [])],
                    year=item.get("year"),
                    doi=normalize_doi((item.get("externalIds") or {}).get("DOI")),
                    url=item["url"],
                    pdf_urls=[item["openAccessPdf"]["url"]]
                    if (item.get("openAccessPdf") or {}).get("url")
                    else [],
                    provider=provider,
                    abstract=plain(item.get("abstract")),
                )
                for item in data["data"]
                if item.get("title") and item.get("url")
            ][:6]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ResearchError("Semantic Scholar 检索结果结构无效") from None

import hashlib
import json
from io import BytesIO
from urllib.parse import quote

import httpx
import pytest
from PIL import Image

from assignment_agent.fake_model import FakeModel
from assignment_agent.grounded_writing import AutomaticWriting, EvidenceFailure, auto_license
from assignment_agent.public_fetch import PublicFetcher
from assignment_agent.research import ResearchCollector, inspect_page
from assignment_agent.research_store import ResearchStore, context_hash
from assignment_agent.research_types import Paper, ResearchError, WebCandidate
from assignment_agent.schemas import AssignmentSpec, RunState
from assignment_agent.web_sources import documentation_figures, resolve_web_paper, web_candidates

PAGE = "https://learn.microsoft.com/en-us/example/components"
REPO = "https://raw.githubusercontent.com/MicrosoftDocs/example/staging/"


def test_jsonld_article_requires_author_and_publication_date():
    candidate = Paper(title="One kernel", url="https://publisher.example/article", provider="web")
    data = {
        "@type": "BlogPosting",
        "author": {"@type": "Person", "name": "Actual Author"},
        "datePublished": "10/17/2018",
        "dateModified": "2022-12-12",
    }

    def raw(value):
        return (
            '<html><head><title>One kernel</title><script type="application/ld+json">'
            + json.dumps(value)
            + "</script></head><body><main>Kernel evidence.</main></body></html>"
        ).encode()

    paper = resolve_web_paper(raw(data), candidate.url, candidate)
    assert paper.authors == ["Actual Author"] and paper.year == 2018
    assert inspect_page(raw(data), candidate.url, paper)[0]
    data.pop("datePublished")
    with pytest.raises(ResearchError, match="发表日期"):
        resolve_web_paper(raw(data), candidate.url, candidate)


def lwn_page(byline):
    return (
        "<html><head><title>Namespaces in operation</title></head><body>"
        "<h1>Namespaces in operation</h1>"
        + byline
        + "<article>Namespaces isolate resources for processes. "
        "The article explains namespace interfaces and their limitations.</article>"
        "<footer>Copyright 2025. Comment last modified January 4, 2024.</footer></body></html>"
    ).encode()


def test_lwn_visible_byline_verifies_original_author_and_year():
    url = "https://lwn.net/Articles/531114/"
    candidate = Paper(title="Namespaces in operation", url=url, provider="model_candidate")
    byline = '<div class="FeatureByline">By <b>Michael Kerrisk</b><br>January 4, 2013</div>'
    paper = resolve_web_paper(lwn_page(byline), url, candidate)
    assert paper.authors == ["Michael Kerrisk"] and paper.year == 2013
    assert inspect_page(lwn_page(byline), url, paper)[0]
    assert not inspect_page(lwn_page(byline.replace("2013", "2014")), url, paper)[0]
    assert not inspect_page(
        lwn_page(byline.replace("Michael Kerrisk", "Other Author")), url, paper
    )[0]


@pytest.mark.parametrize(
    "byline",
    [
        '<div class="FeatureByline">By <b>Michael Kerrisk</b><br></div>',
        '<div class="FeatureByline"><b>Michael Kerrisk</b><br>January 4, 2013</div>',
        '<div class="FeatureByline">By <b>Michael Kerrisk</b><br>February 30, 2013</div>',
        '<div class="FeatureByline">By <b>Alice</b><br>January 4, 2013</div>'
        '<div class="FeatureByline">By <b>Bob</b><br>January 4, 2014</div>',
    ],
)
def test_lwn_does_not_infer_missing_or_ambiguous_metadata_from_footer(byline):
    url = "https://lwn.net/Articles/531114/"
    candidate = Paper(title="Namespaces in operation", url=url, provider="model_candidate")
    with pytest.raises(ResearchError, match="发表日期"):
        resolve_web_paper(lwn_page(byline), url, candidate)


@pytest.mark.parametrize("host", ["other.example", "lwn.net.other.example"])
def test_lwn_byline_fallback_is_limited_to_original_publisher(host):
    url = f"https://{host}/Articles/531114/"
    candidate = Paper(title="Namespaces in operation", url=url, provider="model_candidate")
    byline = '<div class="FeatureByline">By <b>Michael Kerrisk</b><br>January 4, 2013</div>'
    with pytest.raises(ResearchError, match="发表日期"):
        resolve_web_paper(lwn_page(byline), url, candidate)


def page(index=0):
    return (
        '<html><head><meta property="og:title" content="Official components '
        f'{index}"><meta name="ms.date" content="2025-02-20T00:00:00Z">'
        '<meta name="github_feedback_content_git_url" '
        'content="https://github.com/MicrosoftDocs/example/blob/staging/components.md">'
        "</head><body><script>Untrusted code is not evidence</script><main>"
        f"<h1>Official components {index}</h1><p>The operating system includes both user-mode "
        'and kernel-mode components.</p><img src="images/components.png" '
        'alt="Overview diagram of operating system components."></main></body></html>'.replace(
            "</main>",
            '<img src="images/extra.png" alt="Another diagram of system components."></main>',
        )
    ).encode()


@pytest.fixture
def web_network():
    flags = {"license": True, "same_image": True, "changed_image": False}
    calls = []
    output = BytesIO()
    Image.new("RGB", (500, 400), "blue").save(output, "PNG")
    image = output.getvalue()

    def respond(request):
        calls.append(request)
        host = request.headers["host"]
        assert "authorization" not in request.headers
        if host == "html.duckduckgo.com":
            links = "".join(
                f'<a class="result__a" href="//duckduckgo.com/l/?uddg={quote(PAGE + str(i))}">'
                f"Official components {i}</a>"
                for i in range(3)
            )
            return httpx.Response(200, text="<html>" + links + "</html>")
        if request.url.path.endswith("LICENSE"):
            return httpx.Response(
                200,
                text="Attribution 4.0 International\nLicensed material."
                if flags["license"]
                else "All rights reserved.",
            )
        if request.url.path.endswith(".md"):
            return httpx.Response(
                200, text="![Components](images/components.png)\n![Extra](images/extra.png)"
            )
        if request.url.path.endswith(".png"):
            raw = image
            if request.url.path.endswith("extra.png"):
                extra = BytesIO()
                Image.new("RGB", (500, 400), "green").save(extra, "PNG")
                raw = extra.getvalue()
            if flags["changed_image"] or (
                host == "learn.microsoft.com" and not flags["same_image"]
            ):
                raw += b"changed"
            return httpx.Response(200, content=raw, headers={"content-type": "image/png"})
        return httpx.Response(
            200, content=page(int(request.url.path[-1])), headers={"content-type": "text/html"}
        )

    def factory():
        return PublicFetcher(
            transport=httpx.MockTransport(respond),
            resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
        )

    return flags, calls, factory


def test_web_search_unwraps_real_urls_and_metadata_requires_original_source(web_network):
    _, _, factory = web_network
    candidates = web_candidates(factory(), "components")
    assert [p.url for p in candidates] == [PAGE + str(i) for i in range(3)]
    assert all(p.metadata_pending and not p.authors and p.year is None for p in candidates)
    paper = resolve_web_paper(page(), PAGE, candidates[0])
    assert paper.authors == ["Microsoft"] and paper.year == 2025
    assert paper.provider == "microsoft-learn" and paper.doi is None
    with pytest.raises(ResearchError, match="作者及发表日期"):
        resolve_web_paper(page(), "https://untrusted.example/page", candidates[0])
    with pytest.raises(ResearchError, match="标题"):
        resolve_web_paper(page(), PAGE, Paper(title="Different paper", url=PAGE, provider="web"))


def test_web_figure_requires_page_repository_image_and_license_to_agree(tmp_path, web_network):
    flags, _, factory = web_network
    figures = documentation_figures(page(), PAGE, factory(), tmp_path)
    assert len(figures) == 2 and all(auto_license(f) for f in figures)
    figure = figures[0]
    assert figure["page"] is None
    assert figure["image_url"] == REPO + "images/components.png"
    proof = json.loads((tmp_path / figure["provenance_asset"]["filename"]).read_bytes())
    assert proof["source_url"] == REPO + "components.md"
    assert (
        proof["license_sha256"]
        == hashlib.sha256(b"Attribution 4.0 International\nLicensed material.").hexdigest()
    )
    flags["same_image"] = False
    assert documentation_figures(page(), PAGE, factory(), tmp_path) == []
    flags.update(same_image=True, license=False)
    assert documentation_figures(page(), PAGE, factory(), tmp_path) == []
    assert documentation_figures(page(), "https://untrusted.example/", factory(), tmp_path) == []


def test_automatic_writing_uses_persisted_web_evidence_and_checks_remote_image(
    tmp_path, web_network
):
    flags, _, factory = web_network
    run = RunState(
        spec=AssignmentSpec(
            title="Operating system components",
            sections=["Analysis"],
            word_limit=900,
            min_web_images=2,
        ),
        automatic_research=True,
    )
    store = ResearchStore(tmp_path / "agent.sqlite")

    class Search:
        providers = ("web",)

        def search(self, provider, query):
            return web_candidates(factory(), query)

    result = ResearchCollector(store, fetcher=factory(), search=Search()).collect(
        "web", run, ["components"]
    )
    assert sum(p["verified"] for p in result["sources"]) == 3
    assert len(result["figures"]) == 6
    assert all(f["integrity_ok"] for f in result["figures"])
    pipeline = AutomaticWriting(store.database, FakeModel(), fetcher_factory=factory)
    pool = pipeline.pool("web", run)
    assert len(pool["figures"]) == 2
    draft = FakeModel().section_with_evidence(run, {}, pool)
    pipeline.validate(draft, pool)
    ids = {f.figure_id for f in draft.figures}
    pipeline.check_links("web", run, {c.source_id for c in draft.citations})
    pipeline.check_figure_links("web", run, ids)
    content, _ = pipeline.render("web", run, draft, pool)
    assert "PDF p. None" not in content and "Image: " + REPO in content
    body, references = content.split("## References", 1)
    for suffix in ("a", "b", "c"):
        assert f"(Microsoft, 2025{suffix})" in body
        assert f"Microsoft (2025{suffix}). Official components" in references
    flags["changed_image"] = True
    with pytest.raises(EvidenceFailure, match="原图内容已变化"):
        pipeline.check_figure_links("web", run, ids)
    proof = store.entries("web", context_hash(run), "figure")[0]["provenance_asset"]
    (store.assets / proof["filename"]).write_text("changed", encoding="utf-8")
    assert not any(f["integrity_ok"] for f in store.summary("web", run)["figures"])


def test_general_repository_license_cannot_override_notice_inside_figure(
    tmp_path, web_network, monkeypatch
):
    _, _, factory = web_network
    store = ResearchStore(tmp_path / "agent.sqlite")
    run = RunState(spec=AssignmentSpec(title="Components", sections=["Analysis"], word_limit=900))

    class Search:
        providers = ("web",)

        def search(self, provider, query):
            return web_candidates(factory(), query)

    ResearchCollector(store, fetcher=factory(), search=Search()).collect("web", run, ["components"])
    monkeypatch.setattr(
        "assignment_agent.grounded_writing.inspect_image_notice",
        lambda raw: {"version": 1, "status": "blocked", "notices": ["All rights reserved."]},
    )
    pipeline = AutomaticWriting(store.database, FakeModel(), fetcher_factory=factory)
    pool = pipeline.pool("web", run)
    assert len(pool["figures"]) == 2
    selected = {f["id"] for f in pool["figures"]}
    pipeline.check_figure_links("web", run, selected)
    figures = store.entries("web", context_hash(run), "figure")
    reviewed = [f for f in figures if f["id"] in selected]
    assert all(f["license"]["status"] == "needs_review" for f in reviewed)
    assert all(f["image_review"]["notices"] and f["license_warnings"] for f in reviewed)
    assert not any(auto_license(f) for f in reviewed)


def test_model_candidates_require_real_fetch_and_metadata_before_counting(tmp_path, web_network):
    _, calls, factory = web_network
    store = ResearchStore(tmp_path / "agent.sqlite")
    run = RunState(spec=AssignmentSpec(title="Components", sections=["Analysis"], word_limit=900))

    class NoSearch:
        providers = ()

    result = ResearchCollector(store, fetcher=factory(), search=NoSearch()).collect(
        "hints",
        run,
        ["components"],
        model_candidates=[
            WebCandidate(title="Official components 0", url=PAGE + "0"),
            WebCandidate(title="Unsafe local address", url="http://127.0.0.1/private"),
        ],
    )
    assert len(result["sources"]) == 1
    source = result["sources"][0]
    assert source["verified"] and source["authors"] == ["Microsoft"] and source["year"] == 2025
    assert calls and source["provider"] == "model_candidate"
    assert any(e["code"] == "discovery.model_candidate" for e in result["events"])
    assert not any(e["code"] == "search.completed" for e in result["events"])
    assert result["confirmed_sources"] == 0

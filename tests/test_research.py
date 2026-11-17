import hashlib
import json
from io import BytesIO

import httpx
import pytest
from conftest import legacy_agent as open_agent
from conftest import legacy_app as create_app
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.annotations import Link
from pypdf.generic import DictionaryObject, NameObject, StreamObject
from test_web import HEADERS, wait
from test_workflow import decision

from assignment_agent.paper_figures import crop_figure, inspect_paper, save_asset
from assignment_agent.public_fetch import PublicFetcher, public_url
from assignment_agent.research import reconcile_pdf_authors
from assignment_agent.research_store import ResearchStore, context_hash, interrupt_research
from assignment_agent.research_types import LibraryReview, Paper, ResearchError, ResearchRequest
from assignment_agent.schemas import AssignmentSpec, RunState


def paper_pdf(index=0, *, license_text=True, wrong_title=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    title = f"Paper architecture {index}" if not wrong_title else "Unrelated study of biology"
    license_line = (
        "Licensed under CC BY 4.0. https://creativecommons.org/licenses/by/4.0/"
        if license_text
        else "Copyright 2024. All rights reserved."
    )
    stream = StreamObject()
    stream.set_data(
        (
            f"BT /F1 18 Tf 50 750 Td ({title}) Tj ET\n"
            f"BT /F1 12 Tf 50 720 Td (Alice Researcher{index} 2024) Tj ET\n"
            f"BT /F1 10 Tf 50 690 Td ({license_line}) Tj ET\n"
            "BT /F1 12 Tf 80 650 Td (System processes) Tj ET\n"
            f"0.1 0.2 {0.4 + index / 10} rg 80 430 400 210 re f\n"
            f"BT /F1 12 Tf 50 400 Td (Figure 1: Architecture diagram {index}) Tj ET\n"
        ).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def candidate(index=0):
    return Paper(
        title=f"Paper architecture {index}",
        authors=[f"Alice Researcher{index}"],
        year=2024,
        doi=f"10.1234/paper{index}",
        url=f"https://papers.example/article{index}",
        pdf_urls=[f"https://papers.example/paper{index}.pdf"],
        provider="crossref",
    )


def test_pdf_and_doi_agree_on_author_over_stale_search_metadata(tmp_path):
    store = ResearchStore(tmp_path / "authors.sqlite")
    asset = save_asset(
        store.assets,
        json.dumps([{"page": 1, "text": "Alice Researcher, Nida Rashid"}]).encode(),
        "json",
    )
    source = {
        "authors": ["Alice Researcher", "N. Abdul Rashid"],
        "paper": {"authors": ["Alice Researcher", "N. Abdul Rashid"]},
        "text_asset": asset,
        "citation_metadata": {
            "authors": [
                {"given": "Alice", "family": "Researcher"},
                {"given": "Nida", "family": "Rashid"},
            ]
        },
    }
    assert reconcile_pdf_authors(source, store)
    assert source["authors"] == ["Alice Researcher", "Nida Rashid"]
    assert source["paper"]["family_names"] == ["Researcher", "Rashid"]
    source["authors"][-1] = "N. Abdul Rashid"
    source["paper"]["authors"][-1] = "N. Abdul Rashid"
    source["text_asset"] = save_asset(
        store.assets, json.dumps([{"page": 1, "text": "Alice Researcher only"}]).encode(), "json"
    )
    assert not reconcile_pdf_authors(source, store)
    assert source["authors"][-1] == "N. Abdul Rashid"


@pytest.fixture
def research_network():
    calls = []
    flags = {"fail_pdf": set(), "wrong_pdf": set(), "s2_error": False, "no_license": set()}

    def crossref(index):
        p = candidate(index)
        return {
            "title": [p.title],
            "author": [{"given": "Alice", "family": f"Researcher{index}"}],
            "published": {"date-parts": [[2024]]},
            "DOI": p.doi,
            "URL": p.url,
            "container-title": ["Journal of Software Architecture"],
            "volume": "10",
            "issue": "2",
            "page": "41-55",
            "link": [{"URL": p.pdf_urls[0], "content-type": "application/pdf"}],
        }

    def respond(request):
        calls.append(request)
        host = request.headers["host"].split(":")[0]
        assert request.url.host == "93.184.216.34"
        assert request.extensions["sni_hostname"] == host
        assert "authorization" not in request.headers
        if host == "html.duckduckgo.com":
            if flags.get("web_limit"):
                return httpx.Response(429)
            return httpx.Response(200, text="<html><main></main></html>")
        if host == "www.bing.com":
            return httpx.Response(200, text="<rss><channel/></rss>")
        if host == "learn.microsoft.com":
            return httpx.Response(200, json={"results": []})
        if host == "api.crossref.org":
            if request.url.path == "/works":
                return httpx.Response(
                    200, json={"message": {"items": [crossref(i) for i in range(3)]}}
                )
            index = int(request.url.path[-1])
            return httpx.Response(200, json={"message": crossref(index)})
        if host == "api.openalex.org":

            def work(index):
                p = candidate(index)
                return {
                    "id": f"https://openalex.org/W{index}",
                    "title": p.title,
                    "publication_year": p.year,
                    "doi": p.doi,
                    "authorships": [{"author": {"display_name": p.authors[0]}}],
                    "open_access": {"is_oa": True},
                    "locations": [
                        {"pdf_url": p.pdf_urls[0], "landing_page_url": p.url, "is_oa": True}
                    ],
                    "abstract_inverted_index": None,
                }

            if request.url.path == "/works":
                return httpx.Response(200, json={"results": [work(i) for i in range(3)]})
            return httpx.Response(200, json=work(int(request.url.path[-1])))
        if host == "api.semanticscholar.org":
            if flags["s2_error"]:
                return httpx.Response(429, text="upstream-private-detail")
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "title": candidate(i).title,
                            "authors": [{"name": f"Alice Researcher{i}"}],
                            "year": 2024,
                            "url": candidate(i).url,
                            "externalIds": {"DOI": candidate(i).doi},
                            "openAccessPdf": {"url": candidate(i).pdf_urls[0]},
                        }
                        for i in range(3)
                    ]
                },
            )
        index = int(request.url.path.split(".")[0][-1])
        if request.url.path.endswith(".pdf"):
            if index in flags["fail_pdf"]:
                return httpx.Response(503, text="upstream-private-detail")
            return httpx.Response(
                200,
                content=paper_pdf(
                    index,
                    license_text=index != 2 and index not in flags["no_license"],
                    wrong_title=index in flags["wrong_pdf"],
                ),
                headers={"content-type": "application/pdf"},
            )
        return httpx.Response(404)

    def fetcher(budget=40):
        return PublicFetcher(
            transport=httpx.MockTransport(respond),
            resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 443))],
            budget=budget,
        )

    return calls, flags, fetcher


def start(agent):
    return agent.start(
        AssignmentSpec(
            title="Kernel architecture", sections=["Analysis", "Conclusion"], word_limit=900
        ),
        "research",
    )


def approve_library(agent, current):
    for entry in current["research"]["sources"]:
        if entry.get("verified"):
            current = agent.review_source(
                "research",
                LibraryReview(
                    context_hash=current["research"]["context_hash"],
                    kind="source",
                    entry_id=entry["id"],
                    action="accept",
                ),
            )
    for entry in current["research"]["figures"][:2]:
        current = agent.review_source(
            "research",
            LibraryReview(
                context_hash=current["research"]["context_hash"],
                kind="figure",
                entry_id=entry["id"],
                action="accept",
            ),
        )
    return current


def test_pdf_identity_caption_crop_and_license(tmp_path):
    result = inspect_paper(paper_pdf(), candidate(), tmp_path)
    assert result["checks"] == {"title": True, "author": True, "year": True}
    assert result["license"]["status"] == "declared"
    assert len(result["figures"]) == 1
    figure = result["figures"][0]
    assert figure["page"] == 1 and figure["figure_number"] == "1"
    assert figure["caption"].startswith("Figure 1: Architecture")
    assert figure["bbox"][1] < 150  # 上方独立文字标签必须包含在裁剪区域内。
    assert figure["pdf_sha256"] == hashlib.sha256(paper_pdf()).hexdigest()
    assert (tmp_path / figure["filename"]).is_file()
    with pytest.raises(ResearchError, match="不对应"):
        inspect_paper(paper_pdf(wrong_title=True), candidate(), tmp_path)
    with pytest.raises(ResearchError, match="不是 PDF"):
        inspect_paper(b"<html>not a PDF</html>", candidate(), tmp_path)


def test_license_hyperlink_and_tight_figure_caption_are_extracted(tmp_path):
    writer = PdfWriter(BytesIO(paper_pdf()))
    page = writer.pages[0]
    stream = page["/Contents"].get_object()
    content = (
        stream.get_data()
        .replace(
            b"Licensed under CC BY 4.0. https://creativecommons.org/licenses/by/4.0/",
            b"Licensed under Creative Commons Attribution CC BY",
        )
        .replace(b"80 430 400 210 re", b"80 409 400 231 re")
    )
    stream.set_data(content)
    writer.add_annotation(
        0, Link(rect=(50, 685, 400, 705), url="https://creativecommons.org/licenses/by/4.0/")
    )
    buffer = BytesIO()
    writer.write(buffer)
    result = inspect_paper(buffer.getvalue(), candidate(), tmp_path)
    assert len(result["figures"]) == 1
    assert result["license"]["url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert result["license"]["page"] == 1
    assert "hyperlink" in result["license"]["evidence"]


@pytest.mark.parametrize("license_text", [True, False])
def test_long_paper_has_explicit_partial_text_coverage(tmp_path, license_text):
    writer = PdfWriter(BytesIO(paper_pdf(license_text=license_text)))
    for _ in range(120):
        writer.add_blank_page(width=600, height=800)
    output = BytesIO()
    writer.write(output)
    result = inspect_paper(output.getvalue(), candidate(), tmp_path)
    assert result["parsed_pages"] == 120
    assert result["total_pages"] == 121
    assert len(result["figures"]) == 1
    assert any("未对其余页面作核验" in warning for warning in result["warnings"])


def test_collect_deduplicate_confirm_and_resume_without_advancing_graph(tmp_path, research_network):
    calls, _, factory = research_network
    db = tmp_path / "agent.sqlite"
    with open_agent(db) as agent:
        original = start(agent)
        checkpoint = agent.graph.get_state(agent._config("research")).config
        result = agent.collect_sources(
            "research", ResearchRequest(queries=["kernel architecture"]), fetcher=factory()
        )
        assert result["state"] == original["state"]
        assert result["interrupt_id"] == original["interrupt_id"]
        assert agent.graph.get_state(agent._config("research")).config == checkpoint
        library = result["research"]
        assert len(library["sources"]) == 3 and len(library["figures"]) == 3
        assert all(item["verified"] for item in library["sources"])
        assert not library["ready"]
        result = approve_library(agent, result)
        assert result["research"]["confirmed_sources"] == 3
        assert result["research"]["confirmed_images"] == 2
        assert result["research"]["ready"]
        unknown = result["research"]["figures"][2]
        with pytest.raises(ResearchError, match="许可未确定"):
            agent.review_source(
                "research",
                LibraryReview(
                    context_hash=library["context_hash"],
                    kind="figure",
                    entry_id=unknown["id"],
                    action="accept",
                ),
            )
        count = len(calls)
        resumed = agent.collect_sources("research", ResearchRequest(), fetcher=factory())
        assert len(calls) == count
        assert resumed["research"]["ready"]
        assert resumed["state"] == original["state"]
    with open_agent(db) as agent:
        assert agent.recover("research")["research"]["ready"]
        advanced = agent.decide("research", decision(agent.status("research")))
        assert advanced["state"]["pending"]["artifact_id"] == "section-1"


def test_partial_results_and_retry_only_failed_downloads(tmp_path, research_network):
    calls, flags, factory = research_network
    flags["fail_pdf"] = {1}
    with open_agent(tmp_path / "agent.sqlite") as agent:
        original = start(agent)
        result = agent.collect_sources(
            "research", ResearchRequest(queries=["kernel"]), fetcher=factory()
        )
        assert result["research"]["status"] == "partial"
        assert len([s for s in result["research"]["sources"] if s.get("verified")]) == 2
        assert result["state"] == original["state"]
        assert "upstream-private-detail" not in str(result)
        before = len(calls)
        flags["fail_pdf"].clear()
        result = agent.collect_sources("research", ResearchRequest(), fetcher=factory())
        assert all(s["verified"] for s in result["research"]["sources"])
        paths = [request.url.path for request in calls[before:]]
        assert "/works" not in paths
        assert not any("paper0" in path or "paper2" in path for path in paths)


def test_corrupted_assets_are_not_counted_and_can_be_rebuilt(tmp_path, research_network):
    calls, _, factory = research_network
    with open_agent(tmp_path / "agent.sqlite") as agent:
        start(agent)
        current = agent.collect_sources(
            "research", ResearchRequest(queries=["kernel"]), fetcher=factory()
        )
        current = approve_library(agent, current)
        store = ResearchStore(agent.database)
        figure = current["research"]["figures"][0]
        (store.assets / figure["filename"]).write_bytes(b"corrupt")
        assert not agent.status("research")["research"]["ready"]
        count = len(calls)
        current = agent.collect_sources("research", ResearchRequest(), fetcher=factory())
        assert len(calls) == count
        assert current["research"]["ready"]


def test_context_change_and_startup_interrupt_are_explicit(tmp_path, research_network):
    _, _, factory = research_network
    db = tmp_path / "agent.sqlite"
    with open_agent(db) as agent:
        result = start(agent)
        result = agent.collect_sources(
            "research", ResearchRequest(queries=["kernel"]), fetcher=factory()
        )
        store = ResearchStore(db)
        digest = result["research"]["context_hash"]
        store.save_run("research", digest, status="running")
        interrupt_research(db)
        assert agent.status("research")["research"]["status"] == "interrupted"
        revised = agent.decide("research", decision(result, "revise", "增加比较"))
        assert revised["research"]["stale"]
        with pytest.raises(ResearchError, match="版本已变化"):
            agent.review_source(
                "research",
                LibraryReview(
                    context_hash=digest,
                    kind="source",
                    entry_id=result["research"]["sources"][0]["id"],
                    action="accept",
                ),
            )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/",
        "http://169.254.169.254/",
        "http://localhost/",
        "https://example.org/?api_key=secret",
        "https://user:password@example.org/",
    ],
)
def test_unsafe_urls_are_blocked(url):
    with pytest.raises(ResearchError):
        public_url(url)


def test_dns_and_redirect_checks_budget_and_download_size():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    def resolver(*args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    with pytest.raises(ResearchError):
        PublicFetcher(transport=httpx.MockTransport(respond), resolver=resolver).get(
            "https://public.example/"
        )
    assert len(calls) == 1
    with pytest.raises(ResearchError, match="非公开"):
        PublicFetcher(resolver=lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))]).get(
            "https://public.example/"
        )
    with pytest.raises(ResearchError, match="预算"):
        PublicFetcher(budget=0).get("https://public.example/")
    with pytest.raises(ResearchError, match="大小"):
        PublicFetcher(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 101)),
            resolver=resolver,
        ).get("https://public.example/", limit=100)


def test_crop_rejects_out_of_bounds():
    from PIL import Image

    with Image.new("RGB", (400, 400)) as image:
        with pytest.raises(ResearchError, match="越界"):
            crop_figure(image, (100, 100), (-1, 0, 90, 90))


def test_unverified_pdf_never_counts_as_ready(tmp_path, research_network):
    _, flags, factory = research_network
    flags["wrong_pdf"] = {0}
    flags["s2_error"] = True
    with open_agent(tmp_path / "agent.sqlite") as agent:
        start(agent)
        result = agent.collect_sources(
            "research", ResearchRequest(queries=["kernel"]), fetcher=factory()
        )
        bad = next(item for item in result["research"]["sources"] if item["title"].endswith("0"))
        assert not bad["verified"]
        with pytest.raises(ResearchError, match="尚未核对"):
            agent.review_source(
                "research",
                LibraryReview(
                    context_hash=result["research"]["context_hash"],
                    kind="source",
                    entry_id=bad["id"],
                    action="accept",
                ),
            )
        assert not result["research"]["ready"]


def test_web_source_library_without_model_key_and_restart(tmp_path, research_network, monkeypatch):
    from assignment_agent import research

    _, _, factory = research_network
    monkeypatch.setattr(research, "PublicFetcher", factory)
    db = tmp_path / "web.sqlite"
    with open_agent(db) as agent:
        original = start(agent)
    app = create_app(db, tmp_path / "missing.env")
    with TestClient(app) as client:
        original = client.get("/api/runs/research").json()
        assert not client.get("/api/config").json()["configured"]
        assert (
            client.post("/api/runs/research/research", json={"queries": ["kernel"]}).status_code
            == 403
        )
        assert (
            client.post(
                "/api/runs/research/research", headers=HEADERS, json={"queries": ["x" * 201]}
            ).status_code
            == 422
        )
        job = client.post(
            "/api/runs/research/research", headers=HEADERS, json={"queries": ["kernel"]}
        )
        assert job.status_code == 202
        assert job.json()["kind"] == "research"
        assert wait(client, job.json())["status"] == "completed"
        current = client.get("/api/runs/research").json()
        assert current["state"] == original["state"]
        library = current["research"]
        for kind, entries in [("source", library["sources"]), ("figure", library["figures"][:2])]:
            for entry in entries:
                response = client.post(
                    "/api/runs/research/research/review",
                    headers=HEADERS,
                    json={
                        "context_hash": library["context_hash"],
                        "kind": kind,
                        "entry_id": entry["id"],
                        "action": "accept",
                        "permission_confirmed": kind == "figure",
                    },
                )
                assert response.status_code == 200
        current = client.get("/api/runs/research").json()
        assert current["research"]["ready"]
        image = client.get("/api/runs/research/research/assets/" + library["figures"][0]["id"])
        assert image.headers["content-type"] == "image/png"
        assert image.content.startswith(b"\x89PNG")
        pdf = client.get("/api/runs/research/research/assets/" + library["sources"][0]["id"])
        assert pdf.content.startswith(b"%PDF-")
        assert "attachment" in pdf.headers["content-disposition"]
        unknown = library["figures"][2]
        assert (
            client.post(
                "/api/runs/research/research/review",
                headers=HEADERS,
                json={
                    "context_hash": library["context_hash"],
                    "kind": "figure",
                    "entry_id": unknown["id"],
                    "action": "accept",
                },
            ).status_code
            == 400
        )
        assert current["state"] == original["state"]
    with TestClient(create_app(db, tmp_path / "missing.env")) as client:
        restored = client.get("/api/runs/research").json()
        assert restored["research"]["ready"]
        assert restored["state"] == original["state"]


def test_unknown_license_can_only_be_recorded_as_explicit_human_review(tmp_path, research_network):
    _, _, factory = research_network
    with open_agent(tmp_path / "agent.sqlite") as agent:
        start(agent)
        current = agent.collect_sources(
            "research", ResearchRequest(queries=["kernel"]), fetcher=factory()
        )
        figure = current["research"]["figures"][2]
        current = agent.review_source(
            "research",
            LibraryReview(
                context_hash=current["research"]["context_hash"],
                kind="figure",
                entry_id=figure["id"],
                action="accept",
                permission_confirmed=True,
                note="已人工核对本次使用许可，保留核对记录",
            ),
        )
        saved = current["research"]["figures"][2]
        assert saved["license"]["status"] == "unknown"
        assert saved["review"]["permission_confirmed"] is True
        assert saved["review"]["note"]


def test_html_source_and_doi_mismatch_are_separate(tmp_path):
    from assignment_agent.research import ResearchCollector, inspect_page

    content = (
        b'<html><head><meta name="citation_title" content="Paper architecture 0">'
        b'<meta name="citation_author" content="Alice Researcher0">'
        b'<meta name="citation_publication_date" content="2024">'
        b'<meta name="citation_pdf_url" content="/paper0.pdf"></head>'
        b"<body>Paper architecture 0 Alice Researcher0 2024</body></html>"
    )
    valid, evidence, urls = inspect_page(content, "https://papers.example/article0", candidate())
    assert valid and "Researcher0" in evidence
    assert urls == ["https://papers.example/paper0.pdf"]
    invalid = content.replace(
        b"</head>", b'<meta name="citation_doi" content="10.1234/wrong"></head>'
    )
    assert not inspect_page(invalid, "https://papers.example/article0", candidate())[0]

    class Fetcher:
        def json(self, *args):
            return {"message": {"DOI": "10.1234/other", "title": ["Wrong source"]}}

    with pytest.raises(ResearchError, match="DOI"):
        ResearchCollector(ResearchStore(tmp_path / "agent.sqlite"), fetcher=Fetcher()).verify_doi(
            candidate()
        )


def test_resume_can_add_pdf_to_previously_verified_web_source(tmp_path):
    content = (
        b'<html><head><meta name="citation_title" content="Paper architecture 0">'
        b'<meta name="citation_author" content="Alice Researcher0">'
        b'<meta name="citation_publication_date" content="2024"></head>'
        b"<body>Paper architecture 0 Alice Researcher0 2024. "
        b"The architecture separates system service boundaries "
        b"and illustrates operating system processes."
        b"</body></html>"
    )

    class Search:
        providers = ("test",)

        def search(self, provider, query):
            paper = candidate().model_copy(update={"doi": None})
            if query == "initial":
                paper.pdf_urls = []
            return [paper]

    class Fetcher:
        def get(self, url, **kwargs):
            if url.endswith(".pdf"):
                return url, "application/pdf", paper_pdf()
            return url, "text/html", content

    with open_agent(tmp_path / "agent.sqlite") as agent:
        original = start(agent)
        first = agent.collect_sources(
            "research", ResearchRequest(queries=["initial"]), fetcher=Fetcher(), search=Search()
        )
        assert first["research"]["sources"][0]["verified"]
        assert not first["research"]["figures"]
        second = agent.collect_sources(
            "research", ResearchRequest(queries=["followup"]), fetcher=Fetcher(), search=Search()
        )
        assert len(second["research"]["sources"]) == 1
        source = second["research"]["sources"][0]
        assert source["pdf_url"] == candidate().pdf_urls[0]
        assert len(second["research"]["figures"]) == 1
        assert second["state"] == original["state"]
        assert second["interrupt_id"] == original["interrupt_id"]


def test_empty_search_and_request_budget_leave_recoverable_records(tmp_path, research_network):
    _, _, factory = research_network
    with open_agent(tmp_path / "agent.sqlite") as agent:
        original = start(agent)
        current = agent.collect_sources(
            "research", ResearchRequest(queries=["kernel"]), fetcher=factory(budget=1)
        )
        assert current["research"]["status"] == "partial"
        assert not current["research"]["ready"]
        assert current["state"] == original["state"]
        current = agent.collect_sources("research", ResearchRequest(), fetcher=factory())
        assert len(current["research"]["figures"]) == 3


def test_accepting_recommended_outline_does_not_stale_library():
    from assignment_agent.schemas import OutlinePlan, SectionPlan

    state = RunState(
        spec=AssignmentSpec(title="Plan", sections=[], section_mode="recommend", word_limit=800),
        outline_plan=OutlinePlan(
            parts=[SectionPlan(title="Analysis", word_budget=800, purpose="Compare")]
        ),
    )
    before = context_hash(state)
    state.spec.sections = ["Analysis"]
    assert context_hash(state) == before

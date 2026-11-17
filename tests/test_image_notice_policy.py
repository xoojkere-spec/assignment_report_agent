import json
from io import BytesIO

import pytest
from docx import Document
from test_grounded_writing import AutomaticFake, automatic_state
from test_research import research_network as research_network
from test_web_sources import PAGE
from test_web_sources import web_network as web_network

from assignment_agent import research
from assignment_agent.grounded_writing import AutomaticWriting, EvidenceFailure
from assignment_agent.image_policy import image_warnings
from assignment_agent.research import ResearchCollector
from assignment_agent.research_store import ResearchStore, context_hash
from assignment_agent.research_types import Paper
from assignment_agent.service import open_agent
from assignment_agent.web_sources import WEB_FIGURE_VERSION, page_image_links


def test_unknown_license_completes_audits_resumes_and_exports_with_notice(
    tmp_path, research_network, monkeypatch
):
    _, flags, factory = research_network
    flags["no_license"].add(0)
    monkeypatch.setattr(research, "PublicFetcher", factory)

    class Uncertain(AutomaticFake):
        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            for figure in verdict.figures:
                figure.license_applicable = False
            return verdict

    db = tmp_path / "agent.sqlite"
    with open_agent(db, Uncertain()) as agent:
        result = agent.start(automatic_state().spec, "unknown")
        assert result["state"]["status"] == "completed" and result["delivery"]["confirmed"]
        assert result["research"]["used_images"] == 2
        raw, _ = agent.export_docx("unknown")
        doc = Document(BytesIO(raw))
        assert len(doc.inline_shapes) == 2
        assert "Permission or applicable terms" in "\n".join(p.text for p in doc.paragraphs)
        bundle = result["state"]["grounded_drafts"]["section-1"]
        assert not bundle["verdict"]["figures"][0]["license_applicable"]
        figures = [f for f in result["research"]["figures"] if f["id"] in bundle["figure_ids"]]
        assert any(f["license"]["status"] == "unknown" for f in figures)
        assert all(f["license_warnings"] for f in figures)
    with open_agent(db, Uncertain()) as agent:
        assert agent.recover("unknown")["delivery"]["confirmed"]


def test_generic_article_images_have_association_proof_and_remote_checks(tmp_path, web_network):
    flags, _, factory = web_network
    flags["license"] = False
    run = automatic_state()
    store = ResearchStore(tmp_path / "agent.sqlite")
    collector = ResearchCollector(store, fetcher=factory())
    paper = Paper(
        title="Official components 0", url=PAGE + "0", provider="web", metadata_pending=True
    )
    source = {
        "id": "src-" + "1" * 24,
        "paper": paper.model_dump(),
        "title": paper.title,
        "url": paper.url,
        "authors": [],
        "year": None,
        "doi": None,
    }
    collector.inspect_source("web", context_hash(run), source)
    figures = store.entries("web", context_hash(run), "figure")
    assert len(figures) == 2
    assert all(
        f["method"] == "web_page_image" and f["license"]["status"] == "unknown" for f in figures
    )
    for figure in figures:
        proof = json.loads((store.assets / figure["provenance_asset"]["filename"]).read_bytes())
        assert proof["page_url"] == paper.url and proof["image_url"] == figure["image_url"]
    pipeline = AutomaticWriting(store.database, AutomaticFake(), fetcher_factory=factory)
    pipeline.check_figure_links("web", run, {f["id"] for f in figures})
    old_factory = pipeline.fetcher_factory

    class Removed:
        def get(self, url, **kwargs):
            if url == paper.url:
                return (
                    url,
                    "text/html",
                    b"<html><main>" + b"Article body without images. " * 8 + b"</main></html>",
                )
            return old_factory().get(url, **kwargs)

    pipeline.fetcher_factory = Removed
    with pytest.raises(EvidenceFailure, match="不再关联"):
        pipeline.check_figure_links("web", run, {figures[0]["id"]})
    pipeline.fetcher_factory = old_factory
    flags["changed_image"] = True
    with pytest.raises(EvidenceFailure, match="原图内容已变化"):
        pipeline.check_figure_links("web", run, {figures[0]["id"]})


def test_web_image_upgrade_keeps_existing_chapter_source_snapshot(tmp_path, web_network):
    _, _, factory = web_network
    store = ResearchStore(tmp_path / "agent.sqlite")
    run = automatic_state()
    paper = Paper(
        title="Official components 0", url=PAGE + "0", provider="web", metadata_pending=True
    )
    source = {
        "id": "src-" + "2" * 24,
        "paper": paper.model_dump(),
        "title": paper.title,
        "url": paper.url,
        "authors": [],
        "year": None,
        "doi": None,
    }
    ResearchCollector(store, fetcher=factory()).inspect_source("upgrade", context_hash(run), source)
    old = store.entries("upgrade", context_hash(run), "source")[0]
    old["web_extractor_version"] = 1

    class Cached:
        def get(self, url, **kwargs):
            assert url != paper.url, "升级提图不应更换已写章节的网页原文快照"
            return factory().get(url, **kwargs)

    ResearchCollector(store, fetcher=Cached(), search=object()).inspect_source(
        "upgrade", context_hash(run), old
    )
    current = store.entries("upgrade", context_hash(run), "source")[0]
    assert current["page_asset"] == old["page_asset"]
    assert current["text_asset"] == old["text_asset"]
    assert current["web_extractor_version"] == WEB_FIGURE_VERSION
    figures = store.entries("upgrade", context_hash(run), "figure")
    proof_path = store.assets / figures[0]["provenance_asset"]["filename"]
    proof_path.write_bytes(b"changed")
    collector = ResearchCollector(store, fetcher=Cached(), search=object())
    assert not collector.ready(current, figures)
    collector.inspect_source("upgrade", context_hash(run), current)
    restored = store.entries("upgrade", context_hash(run), "figure")
    assert all(store.asset_valid(f["provenance_asset"]) for f in restored)
    assert (
        store.entries("upgrade", context_hash(run), "source")[0]["page_asset"] == old["page_asset"]
    )


def test_article_link_parser_skips_private_urls_icons_and_navigation():
    raw = b"""<html><body><nav>
    <img src="https://ads.example/a.png" alt="Some large navigation image"></nav><main>
    <img src="http://127.0.0.1/x.png" alt="Private image must not load">
    <img src="/icon.png" alt="icon"><figure><img src="/architecture.png" alt="">
    <figcaption>Windows internal architecture components</figcaption>
    </figure></main></body></html>"""
    assert page_image_links(raw, "https://paper.example/article") == {
        "https://paper.example/architecture.png": "Windows internal architecture components"
    }
    warnings = image_warnings({"license": {"status": "unknown", "url": None}})
    assert warnings and "不代表已获授权" in warnings[0]

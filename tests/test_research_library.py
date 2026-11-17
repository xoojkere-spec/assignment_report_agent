import copy
import json

import pytest
from test_grounded_writing import automatic_state
from test_research import candidate
from test_research import research_network as research_network

from assignment_agent.fake_model import FakeModel
from assignment_agent.grounded_writing import AutomaticWriting, EvidenceFailure
from assignment_agent.paper_figures import save_asset
from assignment_agent.research import ResearchCollector, source_id
from assignment_agent.research_store import ResearchStore, context_hash
from assignment_agent.research_types import ResearchError
from assignment_agent.schemas import Artifact


def set_draft(run, content, bundle):
    run.drafts = [Artifact(artifact_id="section-1", version=bundle["version"], content=content)]
    run.grounded_drafts = {"section-1": bundle}


def pipeline(tmp_path, research_network):
    _, _, factory = research_network
    return AutomaticWriting(tmp_path / "agent.sqlite", FakeModel(), fetcher_factory=factory)


def test_candidates_are_not_formal_and_unused_files_are_removed_after_commit(
    tmp_path, research_network
):
    p = pipeline(tmp_path, research_network)
    run = automatic_state()
    collector = ResearchCollector(p.store, fetcher=p.fetcher_factory())
    collector.collect("run", run, ["kernel"])
    paper = candidate(3)
    unused = {
        "id": source_id(paper),
        "paper": paper.model_dump(),
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "url": paper.url,
        "verified": False,
    }
    collector.inspect_source("run", context_hash(run), unused)
    unused = next(
        s for s in p.store.entries("run", context_hash(run), "source") if s["id"] == unused["id"]
    )
    assert p.store.library_summary("run", run)["sources"] == []
    with p.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM research_candidates").fetchone()[0] > 0
        assert c.execute("SELECT COUNT(*) FROM research_library_versions").fetchone()[0] == 0
    content, bundle = p.write("run", run, {}, 1)
    set_draft(run, content, bundle)
    formal = p.store.library_summary("run", run)
    assert {s["id"] for s in formal["sources"]} == set(bundle["sources"])
    assert {f["id"] for f in formal["figures"]} == set(bundle["figure_ids"])
    assert unused["id"] not in bundle["sources"]
    assert not (p.store.assets / unused["pdf"]["filename"]).exists()
    assert not (p.store.assets / unused["text_asset"]["filename"]).exists()
    assert formal["candidate_count"] == 0
    assert {s["id"] for s in bundle["pool"]["sources"]} == set(bundle["sources"])
    assert formal["ready"]
    reopened = ResearchStore(p.store.database).library_summary("run", run)
    assert {s["id"] for s in reopened["sources"]} == set(bundle["sources"])


def test_history_keeps_old_files_and_current_view_uses_exact_draft_version(
    tmp_path, research_network
):
    p = pipeline(tmp_path, research_network)
    run = automatic_state()
    content, bundle = p.write("run", run, {}, 1)
    old_run = run.model_copy(deep=True)
    set_draft(old_run, content, bundle)
    source = p.store.entries("run", context_hash(run), "source")[0]
    old_asset = source["pdf"]
    factory = p.fetcher_factory

    class Updated:
        def json(self, *args):
            return factory().json(*args)

        def get(self, url, **kwargs):
            final, content_type, raw = factory().get(url, **kwargs)
            if url == source["pdf_url"]:
                raw += b"\n% new document revision\n"
            return final, content_type, raw

    p.fetcher_factory = Updated
    with pytest.raises(EvidenceFailure, match="PDF 内容已变化"):
        p.check_links("run", run, [source["id"]])
    run.feedback = "修订正文"
    newer, new_bundle = p.write("run", run, {}, 2)
    set_draft(run, newer, new_bundle)
    latest = p.store.library_summary("run", run)
    assert len(latest["history"]) == 2
    assert next(s for s in latest["sources"] if s["id"] == source["id"])["pdf"] != old_asset
    historical = p.store.library_summary("run", old_run)
    old = next(s for s in historical["sources"] if s["id"] == source["id"])
    assert old["pdf"] == old_asset and p.store.asset_valid(old_asset)
    assert not old["integrity_ok"]


def test_formal_source_and_passed_event_commit_atomically(tmp_path, research_network):
    p = pipeline(tmp_path, research_network)
    run = automatic_state()
    content, bundle = p.write("run", run, {}, 1)
    bad = copy.deepcopy(bundle)
    bad["verdict"]["citations"][0]["supported"] = False
    with p.store.connect() as c:
        before = c.execute("SELECT COUNT(*) FROM writing_evidence").fetchone()[0]
    with pytest.raises(ResearchError, match="不能提交正式来源库"):
        p.store.writing_event(
            "run", context_hash(run), "section-1", 2, status="passed", content=content, evidence=bad
        )
    with p.store.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM writing_evidence").fetchone()[0] == before
        assert (
            c.execute("SELECT COUNT(*) FROM research_library_versions WHERE version=2").fetchone()[
                0
            ]
            == 0
        )


def test_recovery_pool_survives_cleanup_and_restart_without_becoming_formal(
    tmp_path, research_network
):
    p = pipeline(tmp_path, research_network)
    run = automatic_state()
    ResearchCollector(p.store, fetcher=p.fetcher_factory()).collect("run", run, ["kernel"])
    source = p.store.entries("run", context_hash(run), "source")[0]
    p.store.save_run(
        "run",
        context_hash(run),
        status="interrupted",
        pending_pool={"source": [source["id"]], "figure": []},
    )
    p.store.cleanup_interrupted()
    restored = ResearchStore(p.store.database)
    remaining = restored.entries("run", context_hash(run), "source")
    assert [s["id"] for s in remaining] == [source["id"]]
    assert restored.asset_valid(remaining[0]["pdf"])
    assert restored.entries("run", context_hash(run), "figure")
    assert restored.library_summary("run", run)["sources"] == []
    content, bundle = p.write("run", run, {}, 1)
    set_draft(run, content, bundle)
    assert restored.library_summary("run", run)["ready"]


def test_cleanup_protects_shared_assets_and_defers_for_active_downloads(tmp_path):
    store = ResearchStore(tmp_path / "agent.sqlite")
    asset = save_asset(store.assets, b"shared document", "pdf")
    for run_id in ("a", "b"):
        store.save_run(run_id, "ctx", status="partial")
        store.save(run_id, "ctx", "source", {"id": "shared", "pdf": asset})
    store.cleanup_candidates("a", "ctx")
    assert store.asset_valid(asset)
    store.save_run("b", "ctx", status="running")
    store.cleanup_candidates("b", "ctx")
    assert store.asset_valid(asset)
    store.save_run("b", "ctx", status="interrupted")
    store.cleanup_candidates("b", "ctx")
    assert not (store.assets / asset["filename"]).exists()


def test_cleanup_never_deletes_outside_asset_directory(tmp_path):
    store = ResearchStore(tmp_path / "agent.sqlite")
    store.assets.mkdir()
    outside = tmp_path / "important.pdf"
    outside.write_bytes(b"keep")
    with store.connect() as c:
        c.execute("INSERT INTO research_garbage VALUES (?)", ("../important.pdf",))
    store.cleanup_candidates("run", "ctx")
    assert outside.read_bytes() == b"keep"


def test_cleanup_reclaims_orphans_from_interrupted_extraction(tmp_path):
    store = ResearchStore(tmp_path / "agent.sqlite")
    asset = save_asset(store.assets, b"unregistered partial extraction", "pdf")
    store.cleanup_candidates("run", "ctx")
    assert not (store.assets / asset["filename"]).exists()


def test_missing_file_cannot_be_promoted_by_an_old_passed_record(tmp_path, research_network):
    p = pipeline(tmp_path, research_network)
    run = automatic_state()
    content, bundle = p.write("run", run, {}, 1)
    snapshot = next(iter(bundle["sources"].values()))
    (p.store.assets / snapshot["text_asset"]["filename"]).unlink()
    with pytest.raises(ResearchError, match="缓存已缺失"):
        p.store.writing_event(
            "run",
            context_hash(run),
            "section-1",
            2,
            status="passed",
            content=content,
            evidence=bundle,
        )
    with p.store.connect() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM research_library_versions WHERE version=2"
            ).fetchone()[0]
            == 0
        )


def test_legacy_migration_preserves_passed_history_and_resume_candidates(
    tmp_path, research_network
):
    p = pipeline(tmp_path, research_network)
    run = automatic_state()
    content, bundle = p.write("run", run, {}, 1)
    set_draft(run, content, bundle)
    # 模拟升级前的同库数据：已用来源与未采用候选混放，而正文检查点保持不变。
    records = [
        (kind, item)
        for kind in ("source", "figure")
        for item in p.store.entries("run", context_hash(run), kind)
    ]
    with p.store.connect() as c:
        c.execute("CREATE TABLE research_entries AS SELECT * FROM research_candidates WHERE 0")
        for kind, item in records:
            c.execute(
                "INSERT INTO research_entries VALUES (?,?,?,?,?)",
                ("run", context_hash(run), kind, item["id"], json.dumps(item)),
            )
        c.execute("DELETE FROM research_library_versions")
        c.execute("DROP TABLE schema_migrations")
    from assignment_agent.migrations.maintenance import migrate

    # 旧库改为显式维护升级；原来源历史、文件和重复读取断言继续保留。
    migrate(p.store.database, tmp_path / "migration-backup")
    restored = ResearchStore(p.store.database)
    first = restored.library_summary("run", run)
    second = restored.library_summary("run", run)
    assert len(first["sources"]) == len(bundle["sources"])
    assert first["history"] == second["history"]
    assert all(restored.asset_valid(s["pdf"]) for s in first["sources"])
    with restored.connect() as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='research_entries'").fetchone()


@pytest.mark.parametrize("preserve_pending", [False, True])
def test_changed_formal_source_stays_invalid_across_cleanup(
    tmp_path, research_network, preserve_pending
):
    p = pipeline(tmp_path, research_network)
    run = automatic_state()
    _, bundle = p.write("run", run, {}, 1)
    source = p.store.entries("run", context_hash(run), "source")[0]
    source.update(verified=False, text_asset={})
    p.store.save("run", context_hash(run), "source", source)
    p.store.cleanup_candidates("run", context_hash(run), preserve_pending=preserve_pending)
    same = next(
        s for s in p.store.entries("run", context_hash(run), "source") if s["id"] == source["id"]
    )
    assert not same["verified"] and source["id"] in bundle["sources"]


def test_figure_only_parent_is_committed_even_without_body_citation(tmp_path, research_network):
    p = pipeline(tmp_path, research_network)
    run = automatic_state()
    ResearchCollector(p.store, fetcher=p.fetcher_factory()).collect("run", run, ["kernel"])
    pool = p.pool("run", run)
    draft = FakeModel().section_with_evidence(run, {}, pool)
    parent = pool["figures"][0]["source_id"]
    removed = next(c for c in draft.citations if c.source_id == parent)
    draft.citations = [c for c in draft.citations if c.source_id != parent]
    draft.content = draft.content.replace(f"{removed.claim} [[cite:{parent}]]", "")
    verdict = FakeModel().verify_section(run, {}, draft, pool)
    content, order = p.render("run", run, draft, pool)
    bundle = p.save_success("run", run, draft, verdict, pool, content, order, 1)
    p.store.writing_event(
        "run", context_hash(run), "section-1", 1, status="passed", content=content, evidence=bundle
    )
    set_draft(run, content, bundle)
    p.store.cleanup_candidates("run", context_hash(run))
    assert parent not in bundle["citation_ids"]
    assert parent in {s["id"] for s in p.store.library_summary("run", run)["sources"]}


def test_manual_exclusion_survives_removal_of_unused_candidate(tmp_path):
    store = ResearchStore(tmp_path / "agent.sqlite")
    value = {"id": "excluded", "review": {"action": "reject", "note": "用户明确排除"}}
    store.save("run", "ctx", "source", value)
    store.cleanup_candidates("run", "ctx")
    assert store.entries("run", "ctx", "source") == []
    store.save("run", "ctx", "source", {"id": "excluded", "review": None})
    assert store.entries("run", "ctx", "source")[0]["review"]["action"] == "reject"

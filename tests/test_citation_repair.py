import json

import httpx
import pytest
from test_grounded_writing import automatic_state
from test_model_adapter import response, settings
from test_research import research_network as research_network

from assignment_agent.evidence_types import (
    CitationRepair,
    CitationRepairs,
    CitationUse,
    EvidenceDraft,
)
from assignment_agent.fake_model import FakeModel
from assignment_agent.grounded_writing import AutomaticWriting, EvidenceFailure
from assignment_agent.model_adapter import ModelError, ModelFormatError, OpenAICompatibleModel


class RepairingModel(FakeModel):
    writes = 0
    repairs = 0

    def section_with_evidence(self, state, context, pool):
        self.writes += 1
        draft = super().section_with_evidence(state, context, pool)
        draft.citations[0].page = None
        return draft

    def repair_citations(self, state, draft, pool, indices):
        self.repairs += 1
        source = next(s for s in pool["sources"] if s["id"] == draft.citations[0].source_id)
        assert source["medium"] == "pdf" and indices == [0]
        return CitationRepairs(
            repairs=[
                CitationRepair(
                    index=0, quote=draft.citations[0].quote, page=source["passages"][0]["page"]
                )
            ]
        )


def test_local_page_repair_preserves_original_and_requires_semantic_check(
    tmp_path, research_network
):
    _, _, factory = research_network
    model = RepairingModel()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", model, fetcher_factory=factory)
    _, bundle = pipeline.write("repair", automatic_state(), {}, 1)
    assert model.writes == 1 and model.repairs == 1
    assert bundle["draft"]["citations"][0]["page"] == 1
    assert bundle["verdict"]["citations"][0]["supported"]
    with pipeline.store.connect() as db:
        events = [
            json.loads(r[0]) for r in db.execute("SELECT data FROM writing_evidence ORDER BY id")
        ]
    assert [e["status"] for e in events] == ["drafted", "bindings_repaired", "passed"]
    assert events[0]["draft"]["citations"][0]["page"] is None
    assert events[1]["draft"]["content"] == events[0]["draft"]["content"]
    assert (
        events[1]["draft"]["citations"][0]["claim"] == events[0]["draft"]["citations"][0]["claim"]
    )


@pytest.mark.parametrize("error_type", [ModelFormatError, ModelError])
def test_only_completed_response_error_falls_back_to_same_evidence(
    tmp_path, research_network, error_type
):
    _, _, factory = research_network

    def search_count():
        with pipeline.store.connect() as connection:
            return connection.execute("SELECT count(*) FROM research_search_attempts").fetchone()[0]

    class RejectedSelection(RepairingModel):
        fallbacks = 0

        def repair_citations(self, state, draft, pool, indices):
            assert indices == [0]
            self.original = draft.content
            self.pool = pool
            self.searches = search_count()
            raise error_type("引文修复失败")

        def repair_bindings(self, state, draft, pool, issues):
            self.fallbacks += 1
            assert pool is self.pool and draft.content == self.original
            fixed = draft.model_copy(deep=True)
            fixed.citations[0].page = (
                super().repair_citations(state, draft, pool, [0]).repairs[0].page
            )
            return fixed

    model = RejectedSelection()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", model, fetcher_factory=factory)
    if error_type is ModelFormatError:
        _, bundle = pipeline.write("fallback", automatic_state(), {}, 1)
        assert model.fallbacks == 1 and bundle["draft"]["content"] == model.original
        assert bundle["verdict"]["citations"][0]["supported"]
    else:
        with pytest.raises(ModelError, match="引文修复失败"):
            pipeline.write("fallback", automatic_state(), {}, 1)
        assert model.fallbacks == 0
    assert model.writes == 1 and search_count() == model.searches


def test_citation_marker_before_terminal_punctuation_preserves_binding(tmp_path, research_network):
    _, _, factory = research_network

    class InlineMarker(RepairingModel):
        def section_with_evidence(self, state, context, pool):
            draft = super().section_with_evidence(state, context, pool)
            use = draft.citations[0]
            marker = f"[[cite:{use.source_id}]]"
            claim = "正文论断明确限定于同一系统的服务机制"
            draft.content = draft.content.replace(
                f"{use.claim} {marker}", f"{claim} {marker}；下句另行说明。", 1
            )
            use.claim = claim + "。"
            return draft

        def repair_bindings(self, state, draft, pool, issues):
            pytest.fail("句末标点位置应自动识别，只有页码需要修复")

    model = InlineMarker()
    pipeline = AutomaticWriting(tmp_path / "agent.sqlite", model, fetcher_factory=factory)
    _, bundle = pipeline.write("inline", automatic_state(), {}, 1)
    assert model.writes == model.repairs == 1
    assert bundle["verdict"]["citations"][0]["supported"]
    assert bundle["draft"]["citations"][0]["claim"] == "正文论断明确限定于同一系统的服务机制。"
    draft = EvidenceDraft.model_validate(bundle["draft"])
    draft.citations[0].claim = "正文论断明确限定于其他系统的服务机制。"
    with pytest.raises(EvidenceFailure, match="没有绑定正文同段论述"):
        pipeline.validate(draft, bundle["pool"])


def test_repair_cannot_edit_unrequested_records_or_bypass_support(tmp_path, research_network):
    _, _, factory = research_network

    class WrongIndex(RepairingModel):
        def repair_citations(self, state, draft, pool, indices):
            result = super().repair_citations(state, draft, pool, indices)
            result.repairs[0].index = 9
            return result

    pipeline = AutomaticWriting(tmp_path / "wrong.sqlite", WrongIndex(), fetcher_factory=factory)
    with pytest.raises(EvidenceFailure, match="超出了待修复记录"):
        pipeline.write("repair", automatic_state(), {}, 1)
    assert not pipeline.store.summary("repair", automatic_state())["ready"]

    class Unsupported(RepairingModel):
        def verify_section(self, state, context, draft, pool):
            verdict = super().verify_section(state, context, draft, pool)
            verdict.citations[0].supported = False
            verdict.citations[0].reason = "引文虽存在，但不能支持论点"
            return verdict

    model = Unsupported()
    pipeline = AutomaticWriting(tmp_path / "unsupported.sqlite", model, fetcher_factory=factory)
    with pytest.raises(EvidenceFailure, match="不能支持论点"):
        pipeline.write("repair", automatic_state(), {}, 1)
    assert model.repairs == 3
    assert not pipeline.store.summary("repair", automatic_state())["ready"]


@pytest.mark.parametrize("medium,page", [("pdf", 3), ("web", None)])
def test_real_adapter_copies_selected_passage_without_retyping_pdf_characters(medium, page):
    captured = []
    original = (
        "While Mach and its contempo\uffferaries exhibited IPC costs that were above the limit, "
        "L4 improved by more than an order of magni\ufffetude."
    )

    def handler(request):
        captured.append(json.loads(request.content))
        return response(json.dumps({"repairs": [{"index": 0, "passage_index": 1}]}))

    model = OpenAICompatibleModel(settings(), transport=httpx.MockTransport(handler))
    key = "src-" + "0" * 24
    draft = EvidenceDraft(
        content="Original claim",
        citations=[
            CitationUse(
                source_id=key,
                claim="Original claim",
                quote="An incorrectly copied quotation.",
                page=None,
            )
        ],
    )
    pool = {
        "sources": [
            {
                "id": key,
                "medium": medium,
                "passages": [
                    {"page": page, "text": "An unrelated introductory paragraph for comparison."},
                    {"page": page, "text": original},
                ],
            }
        ]
    }
    repaired = model.repair_citations(automatic_state(), draft, pool, [0])
    assert repaired.repairs[0].page == page
    assert repaired.repairs[0].quote == original
    payload = json.loads(captured[0]["messages"][-1]["content"])
    assert payload["task"] == "citation_repair" and payload["sources"][0]["medium"] == medium
    assert payload["sources"][0]["passages"][1]["passage_index"] == 1
    assert "不能跨段拼接" in captured[0]["messages"][0]["content"]
    assert draft.citations[0].page is None
    assert draft.citations[0].claim == "Original claim"
    assert draft.content == "Original claim"

    bound = draft.model_copy(deep=True)
    bound.content += f" [[cite:{key}]]"
    bound.citations[0].quote = repaired.repairs[0].quote
    bound.citations[0].page = repaired.repairs[0].page
    pool.update(
        figures=[],
        targets={"used_sources": [], "used_figures": [], "new_sources": 0, "new_figures": 0},
    )
    pipeline = AutomaticWriting.__new__(AutomaticWriting)
    pipeline.validate(bound, pool)
    bound.citations[0].quote = original.replace("were", "are")
    with pytest.raises(EvidenceFailure, match="证据原文或页码不匹配"):
        pipeline.validate(bound, pool)


@pytest.mark.parametrize(
    "choices",
    [
        [{"index": 1, "passage_index": 0}],
        [{"index": 0, "passage_index": 0}, {"index": 0, "passage_index": 0}],
        [{"index": 0, "passage_index": 1}],
    ],
)
def test_passage_selection_rejects_wrong_duplicate_and_out_of_source_indices(choices):
    model = OpenAICompatibleModel(
        settings(),
        transport=httpx.MockTransport(lambda request: response(json.dumps({"repairs": choices}))),
    )
    key = "src-" + "0" * 24
    other = "src-" + "1" * 24
    draft = EvidenceDraft(
        content="Original claim",
        citations=[
            CitationUse(
                source_id=key, claim="Original claim", quote="An incorrectly copied quotation."
            )
        ],
    )
    pool = {
        "sources": [
            {
                "id": key,
                "passages": [{"page": 2, "text": "The original source has one paragraph."}],
            },
            {
                "id": other,
                "passages": [
                    {"page": 1, "text": "Other source first paragraph."},
                    {"page": 8, "text": "Other source second paragraph."},
                ],
            },
        ]
    }
    with pytest.raises(ModelFormatError, match="核验失败"):
        model.repair_citations(automatic_state(), draft, pool, [0])
    assert draft.citations[0].page is None


def test_rejected_binding_patch_is_saved_without_accepting_it(tmp_path, research_network):
    _, _, factory = research_network

    class InvalidPatch(RepairingModel):
        def repair_citations(self, state, draft, pool, indices):
            result = super().repair_citations(state, draft, pool, indices)
            result.repairs[0].quote = "This invented quotation does not exist in the source."
            return result

    pipeline = AutomaticWriting(
        tmp_path / "rejected.sqlite", InvalidPatch(), fetcher_factory=factory
    )
    with pytest.raises(EvidenceFailure, match="证据原文或页码不匹配"):
        pipeline.write("rejected", automatic_state(), {}, 1)
    with pipeline.store.connect() as db:
        events = [
            json.loads(r[0]) for r in db.execute("SELECT data FROM writing_evidence ORDER BY id")
        ]
    rejected = [e for e in events if e["status"] == "bindings_rejected"]
    originals = [e for e in events if e["status"] == "drafted"]
    assert len(rejected) == 3 and len(originals) == 1
    assert not any(e["status"] == "passed" for e in events)
    original = originals[0]
    for repair in rejected:
        assert repair["draft"]["content"] == original["draft"]["content"]
        assert (
            repair["draft"]["citations"][0]["claim"] == original["draft"]["citations"][0]["claim"]
        )
        assert "invented quotation" in repair["draft"]["citations"][0]["quote"]
        assert repair["issues"]


def test_unique_citation_prefix_is_expanded_with_original_saved_and_semantic_check(
    tmp_path, research_network
):
    _, _, factory = research_network

    class ShortMarkers(FakeModel):
        checked = False

        def section_with_evidence(self, state, context, pool):
            draft = super().section_with_evidence(state, context, pool)
            for use in draft.citations:
                draft.content = draft.content.replace(
                    f"[[cite:{use.source_id}]]", f"[[cite:{use.source_id[:12]}]]"
                )
            return draft

        def verify_section(self, state, context, draft, pool):
            self.checked = True
            assert all(f"[[cite:{use.source_id}]]" in draft.content for use in draft.citations)
            return super().verify_section(state, context, draft, pool)

    model = ShortMarkers()
    pipeline = AutomaticWriting(tmp_path / "markers.sqlite", model, fetcher_factory=factory)
    _, bundle = pipeline.write("markers", automatic_state(), {}, 1)
    assert model.checked and bundle["verdict"]["citations"][0]["supported"]
    with pipeline.store.connect() as db:
        events = [
            json.loads(r[0]) for r in db.execute("SELECT data FROM writing_evidence ORDER BY id")
        ]
    assert [e["status"] for e in events] == ["drafted", "markers_expanded", "passed"]
    assert events[0]["draft"]["citations"] == events[1]["draft"]["citations"]
    for prefix, identity in events[1]["mapping"].items():
        assert f"[[cite:{prefix}]]" in events[0]["draft"]["content"]
        assert f"[[cite:{identity}]]" in events[1]["draft"]["content"]


@pytest.mark.parametrize("fault", ["unknown", "ambiguous", "unbound", "invented_quote"])
def test_marker_expansion_never_invents_binding_or_ignores_quotation(fault):
    key = "src-01234567" + "a" * 16
    prefix = key[:12]
    quote = "The original passage about isolation and kernel services."
    draft = EvidenceDraft(
        content=f"Original claim [[cite:{prefix}]]",
        citations=[CitationUse(source_id=key, claim="Original claim", quote=quote, page=1)],
    )
    pool = {
        "sources": [{"id": key, "passages": [{"page": 1, "text": quote}]}],
        "figures": [],
        "targets": {"used_sources": [], "used_figures": [], "new_sources": 0, "new_figures": 0},
    }
    if fault == "unknown":
        draft.content = "Original claim [[cite:src-ffffffff]]"
    elif fault == "ambiguous":
        pool["sources"].append({"id": "src-01234567" + "b" * 16, "passages": []})
    elif fault == "unbound":
        draft.citations[0].source_id = "src-" + "c" * 24
    else:
        draft.citations[0].quote = "This quotation is fabricated and absent from the source."
    expanded, mapping = AutomaticWriting.expand_citation_markers(draft, pool)
    if fault != "invented_quote":
        assert mapping == {} and expanded.content == draft.content
    with pytest.raises(EvidenceFailure):
        AutomaticWriting.__new__(AutomaticWriting).validate(expanded, pool)

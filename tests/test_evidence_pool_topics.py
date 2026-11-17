from assignment_agent.grounded_writing import select_pool_sources


def sources():
    items = [
        {
            "id": f"src-{index:024x}",
            "title": f"Linux kernel security study {index}",
            "passages": [{"page": 1, "text": "Linux module security isolation prototype study"}],
        }
        for index in range(11)
    ]
    core = {
        "id": "src-" + "a" * 24,
        "title": "Windows hybrid design performance",
        "passages": [
            {"page": 1, "text": "Windows hybrid executive kernel design motivation performance"}
        ],
    }
    mitigation = {
        "id": "src-" + "b" * 24,
        "title": "Windows memory integrity",
        "passages": [{"page": 1, "text": "Windows HVCI memory integrity VBS hardware cost"}],
    }
    return [*items, core, mitigation], core, mitigation


def test_existing_core_evidence_and_new_mitigation_survive_single_topic_crowding():
    items, core, mitigation = sources()
    targets = {"new_sources": 0, "new_figures": 0, "used_sources": [core["id"]]}
    selected = select_pool_sources(
        items,
        targets,
        [
            "Linux kernel security isolation",
            "site:learn.microsoft.com Windows hybrid design motivation",
            "Windows HVCI memory integrity cost",
        ],
    )
    assert len(selected) == 10 and len({item["id"] for item in selected}) == 10
    assert core in selected and mitigation in selected
    assert any("Linux" in item["title"] for item in selected)


def test_new_source_quota_keeps_old_core_and_other_topics():
    items, core, mitigation = sources()
    focus = ["Windows hybrid design motivation", "Windows HVCI memory integrity"]
    targets = {"new_sources": 3, "new_figures": 0, "used_sources": [core["id"]]}
    selected = select_pool_sources(items, targets, focus)
    assert len(selected) == 10 and len({item["id"] for item in selected}) == 10
    assert len({item["id"] for item in selected} - set(targets["used_sources"])) >= 3
    assert core in selected and mitigation in selected


def test_verified_prior_chapter_sources_survive_new_source_quota_and_keep_budget():
    items, core, mitigation = sources()
    targets = {"new_sources": 1, "new_figures": 0, "used_sources": [core["id"]]}
    selected = select_pool_sources(
        items, targets, ["Linux kernel security"], preferred_ids=[core["id"], mitigation["id"]]
    )
    assert len(selected) == 10 and len({item["id"] for item in selected}) == 10
    assert core in selected and mitigation in selected
    assert selected[0] == mitigation


def test_image_quota_does_not_hide_text_only_evidence():
    items, core, mitigation = sources()
    selected = select_pool_sources(
        items,
        {"new_sources": 0, "new_figures": 2},
        ["Windows hybrid design motivation", "Windows HVCI memory integrity"],
        figure_source_ids=[item["id"] for item in items[:11]],
    )
    assert items[0] in selected and items[1] in selected
    assert core in selected and mitigation in selected
    assert len(selected) == 10 and len({s["id"] for s in selected}) == 10


def test_source_and_image_quotas_share_slots_without_duplicate_sources():
    items, core, mitigation = sources()
    targets = {"new_sources": 3, "new_figures": 2, "used_sources": [items[0]["id"], core["id"]]}
    selected = select_pool_sources(
        items,
        targets,
        ["Windows hybrid design"],
        preferred_ids=[core["id"], mitigation["id"]],
        figure_source_ids=[items[0]["id"], items[1]["id"]],
    )
    assert items[0] in selected and items[1] in selected
    assert len({s["id"] for s in selected} - set(targets["used_sources"])) >= 3
    assert core in selected and mitigation in selected
    assert len(selected) == 10 and len({s["id"] for s in selected}) == 10


def test_two_distinct_images_from_one_source_need_only_one_slot():
    items, core, mitigation = sources()
    selected = select_pool_sources(
        items,
        {"new_sources": 0, "new_figures": 2},
        ["Windows"],
        figure_source_ids=[items[0]["id"], items[0]["id"], items[1]["id"]],
        limit=3,
    )
    assert selected == [items[0], core, mitigation]


def test_unfocused_order_is_preserved_without_quotas():
    items, _, _ = sources()
    assert select_pool_sources(items, {"new_sources": 0, "new_figures": 0}, []) == items[:10]

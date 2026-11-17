"""自动检索、证据写作、写后核验与有界修复，不把来源确认交给用户。"""

import hashlib
import json
import math
import re
from collections import defaultdict
from urllib.parse import quote

from PIL import Image

from assignment_agent import research as retrieval
from assignment_agent.evidence_types import (
    EVIDENCE_POLICY_VERSION,
    EvidenceDraft,
    EvidenceGap,
    EvidenceVerdict,
)
from assignment_agent.image_policy import IMAGE_POLICY, WEB_IMAGE_METHODS, image_warnings
from assignment_agent.image_policy import auto_license as auto_license
from assignment_agent.image_review import IMAGE_REVIEW_VERSION, inspect_image_notice
from assignment_agent.model_adapter import ModelError, ModelFormatError
from assignment_agent.outline_revision import OutlineReviser, plan_hash, restore_revisions
from assignment_agent.research_budget import SearchBudget
from assignment_agent.research_search import normalized
from assignment_agent.research_store import ResearchStore, context_hash, timestamp
from assignment_agent.research_types import EvidenceFailure, MissingEvidence, Paper, ResearchError
from assignment_agent.schemas import Approval, required_web_images
from assignment_agent.web_sources import page_image_links

CITE = re.compile(r"\[\[cite:(src-[0-9a-f]{24})\]\]")
FIGURE = re.compile(r"\[\[figure:(fig-[0-9a-f]{24})\]\]")
SHORT_CITE = re.compile(r"\[\[cite:(src-[0-9a-f]{8,23})\]\]")
BINDING_MARKER = re.compile(r"\[\[(?:cite|figure):[^\r\n]*?\]\]")
NAMED_TOPIC = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z][A-Za-z0-9-]*(?:[ \t]+[A-Z][A-Za-z0-9-]*)+"
    r"|[A-Z][A-Z0-9]{2,})(?![A-Za-z0-9])"
)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def flat(text):
    return " ".join(text.split())


def unmarked_paragraphs(content):
    return [flat(p) for p in re.split(r"\n\s*\n", BINDING_MARKER.sub("", content)) if p.strip()]


def claim_bound(paragraph, claim, marker):
    if marker not in paragraph:
        return False
    if claim in paragraph:
        return True
    # 标记可放在句末标点之前，核对标记紧邻的逐字论断。
    punctuation = "。.!?！？；;,，：:"
    core = claim.rstrip(punctuation).rstrip()
    if not core or core == claim:
        return False
    closing = set(punctuation)
    return any(
        paragraph[: match.start()].rstrip().endswith(core)
        and paragraph[match.end() :].lstrip()[:1] in closing
        for match in re.finditer(re.escape(marker), paragraph)
    )


def select_passages(passages, topics):
    """保留总分最高的段落，为不同的对象小节各留一处证据。"""
    selected = passages[:1]
    for topic in topics:
        heading_matches = [p for p in passages if topic in p["headings"] and p["mentions"][topic]]
        if heading_matches:
            best = max(heading_matches, key=lambda p: (p["mentions"][topic], p["score"]))
            if best not in selected:
                selected.append(best)
                if len(selected) == 3:
                    break
    return (selected + [p for p in passages if p not in selected])[:4]


def select_pool_sources(sources, targets, focus, limit=10, preferred_ids=(), figure_source_ids=()):
    """仅为新增来源和图片预留必要位置，其余兼顾旧稿线索和各检索主题。"""
    selected = []
    seen = set()

    def add(item):
        if item["id"] not in seen and len(selected) < limit:
            selected.append(item)
            seen.add(item["id"])

    available = {item["id"]: item for item in sources}
    # 每项代表一张不同且未使用的图片；同一来源可拥有多图，但不能挤占全部文字名额。
    for key in figure_source_ids:
        if sum(identity in seen for identity in figure_source_ids) >= targets["new_figures"]:
            break
        if key in available:
            add(available[key])
    preferred = [item for key in preferred_ids for item in sources if item["id"] == key]
    used = set(targets.get("used_sources", []))
    new = [item for item in [*preferred, *sources] if item["id"] not in used]
    for item in new:
        if len(seen - used) >= targets["new_sources"]:
            break
        add(item)
    # 历史来源必须先通过当前完整性与身份筛选；进入池后仍要重新核对正文论据。
    for item in preferred:
        add(item)
    terms = {
        item["id"]: set(
            normalized(item["title"] + " " + " ".join(p["text"] for p in item["passages"])).split()
        )
        for item in sources
    }
    topics = []
    stop = {"a", "an", "the", "and", "or", "of", "for", "in", "with", "to", "from"}
    for query in focus or []:
        query = re.sub(r"site:\S+|https?://\S+", "", query)
        words = set(normalized(query).split()) - stop
        if not words:
            continue
        weights = {
            word: 1 + math.log((len(sources) + 1) / (1 + sum(word in t for t in terms.values())))
            for word in words
        }
        scored = [
            (item, sum(weights[word] for word in words & terms[item["id"]])) for item in sources
        ]
        topics.append([item for item, score in sorted(scored, key=lambda p: -p[1]) if score > 0])
    # 先让每个查询有代表，再补第二份互证资料；相同来源可同时覆盖多个查询。
    for position in range(2):
        for topic in topics:
            if len(topic) <= position:
                continue
            item = topic[position]
            add(item)
            if len(selected) == limit:
                return selected
    return (selected + [item for item in sources if item["id"] not in seen])[:limit]


def reference_title(title):
    return bool(re.fullmatch(r"(?:\d+[.、\s]*)?(?:references|bibliography|参考文献)", title, re.I))


def contents_title(title):
    return bool(
        re.fullmatch(r"(?:\d+[.、\s]*)?(?:table of contents|contents|目录|目次)", title, re.I)
    )


def contents_text(run):
    entries = [
        title for title in run.spec.sections[run.current_index + 1 :] if not contents_title(title)
    ]
    return (
        "\n".join(f"{index}. {title}" for index, title in enumerate(entries, 1)) or "（无后续章节）"
    )


def conclusion_title(title):
    return bool(re.search(r"conclusion|concluding|总结|结论", title, re.I))


def administrative_title(title):
    return bool(
        re.fullmatch(
            r"(?:\d+[.、\s]*)?(?:cover(?: page)?.*|declaration(?: form)?|marking rubrics?"
            r"|peer assessment(?: and workload matrix)?|workload matrix"
            r"|appendix|appendices|封面.*|声明(?:表)?|同伴评价.*|工作量矩阵|附录)",
            title,
            re.I,
        )
    )


def body_indices(sections):
    body = [
        index
        for index, title in enumerate(sections)
        if not any(
            check(title)
            for check in (reference_title, contents_title, conclusion_title, administrative_title)
        )
    ]
    return body or [
        index
        for index, title in enumerate(sections)
        if not reference_title(title) and not contents_title(title)
    ]


def authors(source):
    families = (source.get("paper") or {}).get("family_names", [])
    # 姓氏必须来自明确元数据；未知时使用完整署名，不再猜最后一个词就是姓。
    names = [
        families[index] if index < len(families) and families[index] else name
        for index, name in enumerate(source["authors"])
        if name.strip()
    ]
    if len(names) > 2:
        return names[0] + " et al."
    return " & ".join(names) or "Unknown author"


def numeric_style(spec):
    return bool(re.search(r"ieee|vancouver|numeric|数字|顺序编码", spec.citation_style or "", re.I))


def citation_years(spec, order, sources):
    years = {key: str(sources[key]["year"]) for key in order}
    if numeric_style(spec):
        return years
    groups = defaultdict(list)
    for key in order:
        groups[(normalized(authors(sources[key])), years[key])].append(key)
    for group in groups.values():
        if len(group) < 2:
            continue
        for index, key in enumerate(
            sorted(group, key=lambda k: (normalized(sources[k]["title"]), k)), 1
        ):
            # 同作者同年按标题稳定编号，正文与参考文献共用该年份标签。
            suffix = ""
            while index:
                index, digit = divmod(index - 1, 26)
                suffix = chr(ord("a") + digit) + suffix
            years[key] += suffix
    return years


def citation_label(source, spec, order, years=None):
    if numeric_style(spec):
        return f"[{order.index(source['id']) + 1}]"
    year = years[source["id"]] if years is not None else source["year"]
    return f"({authors(source)}, {year})"


def used_references(bundles):
    order = []
    for bundle in bundles:
        figure_sources = {
            item["id"]: item["source_id"] for item in bundle.get("pool", {}).get("figures", [])
        }
        order.extend(bundle["citation_ids"])
        order.extend(figure_sources[key] for key in bundle["figure_ids"])
    return list(dict.fromkeys(order))


def bibliography(spec, source_ids, sources, *, heading=True):
    title = (
        "参考文献" if re.search(r"中文|chinese|^zh", spec.language or "", re.I) else "References"
    )
    lines = [f"## {title}", ""] if heading else []
    numeric = numeric_style(spec)
    years = citation_years(spec, source_ids, sources)
    ordered = (
        source_ids
        if numeric
        else sorted(
            source_ids,
            key=lambda key: (
                normalized(authors(sources[key])),
                sources[key]["year"] or 0,
                normalized(sources[key]["title"]),
                key,
            ),
        )
    )
    for index, key in enumerate(ordered, 1):
        source = sources[key]
        if re.search(r"\bapa\b", spec.citation_style or "", re.I):
            metadata = source.get("citation_metadata") or {}
            people = metadata.get("authors") or []
            names = []
            for person in people:
                initials = " ".join(
                    word[0].upper() + "."
                    for word in re.findall(r"[^\W\d_]+", person.get("given", ""))
                )
                names.append(", ".join(part for part in (person["family"], initials) if part))
            if not names:
                # 索引未提供结构化姓氏时，保留原署名，避免凭词序猜测姓氏。
                names = source["authors"]
            author_text = (
                names[0] if len(names) == 1 else ", ".join(names[:-1]) + ", & " + names[-1]
            )
            line = f"{author_text} ({years[key]}). {source['title']}."
            journal = metadata.get("journal")
            if journal:
                volume = metadata.get("volume")
                issue = metadata.get("issue")
                publication = f"*{journal}{', ' + volume if volume else ''}*"
                if issue:
                    publication += f"({issue})"
                locator = metadata.get("page") or metadata.get("article_number")
                if locator:
                    publication += ", " + locator.replace("-", "–")
                line += f" {publication}."
            line += " " + (
                f"https://doi.org/{source['doi']}" if source.get("doi") else source["url"]
            )
        else:
            line = f"{'; '.join(source['authors'])} ({years[key]}). {source['title']}."
            if source.get("doi"):
                line += f" DOI: {source['doi']}."
            line += " " + (source.get("pdf_url") or source["url"])
        lines.extend([f"[{index}] {line}" if numeric else line, ""])
    return "\n".join(lines).strip()


class LocalRepairFailure(EvidenceFailure):
    """格式、标记或绑定错误须在当前草稿与证据池内修复。"""


class AutomaticWriting:
    attempts = 3

    def __init__(self, database, model, *, fetcher_factory=None):
        self.store = ResearchStore(database)
        self.model = model
        self.fetcher_factory = fetcher_factory or retrieval.PublicFetcher
        self.audit_repairs = 0

    @staticmethod
    def enabled(run):
        return run.automatic_research or run.model_profile.kind == "openai_compatible"

    def pages(self, source):
        if not self.store.asset_valid(source.get("text_asset")):
            raise EvidenceFailure(["来源全文证据缺失或变化，需重新获取"])
        return json.loads((self.store.assets / source["text_asset"]["filename"]).read_bytes())

    def previous(self, run):
        return [
            run.grounded_drafts[item.artifact_id]
            for item in run.drafts[: run.current_index]
            if item.artifact_id in run.grounded_drafts
        ]

    def targets(self, run):
        previous = self.previous(run)
        retained = {}
        if run.current_index + 1 < len(run.drafts):
            approved = run.approved_versions()
            for index in range(run.current_index + 1, len(run.drafts)):
                draft = run.drafts[index]
                bundle = run.grounded_drafts.get(draft.artifact_id)
                if (
                    (draft.artifact_id, draft.version) in approved
                    and bundle
                    and bundle["version"] == draft.version
                    and bundle["content_hash"] == digest(draft.content)
                ):
                    retained[index] = bundle
        # 修复前章时计入保留稿已用的来源和插图，避免为同一全篇指标重新搜索。
        used = [*previous, *retained.values()]
        used_sources = {key for item in used for key in item["citation_ids"]}
        used_figures = {key for item in used for key in item["figure_ids"]}
        body = body_indices(run.spec.sections)
        remaining = [i for i in body if i >= run.current_index and i not in retained]
        picture_sections = []
        if run.outline_plan:
            picture_sections = [
                i
                for i, part in enumerate(run.outline_plan.parts)
                if i in body
                and re.search(
                    r"diagram|illustrat|插图|架构图|流程图|结构图",
                    " ".join([part.purpose, *part.evidence_needs]),
                    re.I,
                )
            ]
        if not picture_sections:
            picture_sections = [
                i
                for i in body
                if not re.search(r"introduction|roadmap|引言|绪论", run.spec.sections[i], re.I)
            ] or body
        picture_slots = [i for i in picture_sections if i in remaining]
        image_minimum = required_web_images(run)
        new_figures = 0
        if run.current_index in picture_slots:
            new_figures = math.ceil(max(0, image_minimum - len(used_figures)) / len(picture_slots))
        elif remaining == [run.current_index] and not picture_slots:
            new_figures = max(0, image_minimum - len(used_figures))
        # 来源下限属于全文，只有最后一个论证章节承担尚未完成的差额。
        new_sources = (
            max(0, run.spec.min_sources - len(used_sources))
            if body and run.current_index == body[-1]
            else 0
        )
        return {
            "new_sources": new_sources,
            "new_figures": new_figures,
            "image_goal_optional": image_minimum < run.spec.min_web_images,
            "section_role": (
                "contents"
                if contents_title(run.spec.sections[run.current_index])
                else "body"
                if run.current_index in body
                else "administrative"
                if administrative_title(run.spec.sections[run.current_index])
                else "conclusion"
            ),
            "source_topup": bool(new_sources),
            "used_sources": sorted(used_sources),
            "used_figures": sorted(used_figures),
        }

    def pool(self, run_id, run, focus=None, *, summary=None):
        if summary is None:
            summary = self.store.summary(run_id, run)
        targets = self.targets(run)
        topic = normalized(run.spec.title + " " + run.spec.sections[run.current_index])
        words = set(topic.split())
        needs = []
        if run.outline_plan:
            part = run.outline_plan.parts[run.current_index]
            needs = [part.purpose, *part.evidence_needs]
        gap_text = " ".join([run.feedback, *needs, *(focus or [])])
        named_topics = list(
            dict.fromkeys(normalized(item) for item in NAMED_TOPIC.findall(gap_text))
        )
        named_keys = set(named_topics)
        # 拉丁术语常嵌在中文反馈中，单独提取以避免整串中文吞掉 NT、microkernel 等词。
        gap_words = set(normalized(gap_text).split()) | {
            w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", gap_text)
        }
        figure_scores = {}
        for figure in summary["figures"]:
            if figure["integrity_ok"] and (figure.get("review") or {}).get("action") != "reject":
                score = len(
                    gap_words & set(normalized(figure["caption"].replace("_", " ")).split())
                )
                figure_scores[figure["source_id"]] = max(
                    figure_scores.get(figure["source_id"], 0), score
                )
        rankings = {}
        sources = []
        seen = set()
        for source in summary["sources"]:
            identity = (source.get("pdf") or {}).get("sha256") or source.get("doi") or source["url"]
            if (
                not source.get("verified")
                or source.get("retired")
                or not source["integrity_ok"]
                or not self.store.asset_valid(source.get("text_asset"))
                or (
                    not source.get("pdf")
                    and source.get("web_text_version") != retrieval.WEB_TEXT_VERSION
                )
                or (source.get("review") or {}).get("action") == "reject"
                or identity in seen
                or (
                    targets["section_role"] == "conclusion"
                    and source["id"] not in targets["used_sources"]
                )
            ):
                continue
            passages = []
            for page in self.pages(source):
                text = flat(page["text"])
                headings = (
                    {
                        re.sub(r"^(?:\d+ )+", "", normalized(line))
                        for line in page["text"].splitlines()
                        if len(line) <= 120
                    }
                    & named_keys
                    if named_keys
                    else set()
                )
                for start in range(0, len(text), 1000):
                    passage = text[max(0, start - 100) : start + 1200]
                    if len(passage) >= 20:
                        body = normalized(passage)
                        terms = set(body.split())
                        score = 4 * len(gap_words & terms) + len(words & terms)
                        mentions = {key: f" {body} ".count(f" {key} ") for key in headings}
                        passages.append(
                            {
                                "page": page["page"],
                                "text": passage,
                                "score": score,
                                "headings": headings,
                                "mentions": mentions,
                            }
                        )
            passages.sort(key=lambda item: item["score"], reverse=True)
            if not passages:
                continue
            seen.add(identity)
            public = {
                key: source.get(key)
                for key in ("id", "title", "authors", "year", "doi", "url", "pdf_url")
            }
            public["passages"] = [
                {"page": item["page"], "text": item["text"]}
                for item in select_passages(passages, named_topics)
            ]
            public["citation_author"] = authors(source)
            public["medium"] = "pdf" if source.get("pdf") else "web"
            rankings[source["id"]] = (
                -(passages[0]["score"] if passages else 0),
                source.get("selection_rank") if source.get("selection_rank") is not None else 1000,
            )
            sources.append(public)
        sources.sort(key=lambda item: rankings[item["id"]])
        available = {item["id"] for item in sources}
        figure_sources = []
        figure_hashes = set()
        for figure in sorted(
            summary["figures"], key=lambda item: -figure_scores.get(item["source_id"], 0)
        ):
            if (
                figure["source_id"] in available
                and figure["integrity_ok"]
                and figure["id"] not in targets["used_figures"]
                and figure["sha256"] not in figure_hashes
                and (figure.get("review") or {}).get("action") != "reject"
            ):
                figure_hashes.add(figure["sha256"])
                figure_sources.append(figure["source_id"])
        sources = select_pool_sources(
            sources,
            targets,
            focus,
            preferred_ids=self.store.passed_source_ids(
                run_id, context_hash(run), f"section-{run.current_index + 1}"
            ),
            figure_source_ids=figure_sources,
        )
        source_ids = {item["id"] for item in sources}
        figures = []
        image_hashes = set()
        for figure in summary["figures"]:
            if (
                figure["source_id"] in source_ids
                and figure["integrity_ok"]
                and figure["sha256"] not in image_hashes
                and (figure.get("review") or {}).get("action") != "reject"
            ):
                image_hashes.add(figure["sha256"])
                figures.append(
                    {
                        "license_warnings": image_warnings(figure),
                        **{
                            key: figure[key]
                            for key in (
                                "id",
                                "source_id",
                                "page",
                                "figure_number",
                                "caption",
                                "license",
                                "pdf_url",
                                "source_url",
                                "method",
                            )
                            if key in figure
                        },
                    }
                )
        figures.sort(
            key=lambda item: (
                item["id"] in targets["used_figures"],
                -len(gap_words & set(normalized(item["caption"].replace("_", " ")).split())),
                item.get("method") in WEB_IMAGE_METHODS,
            )
        )
        return {
            "sources": sources,
            "figures": figures[:12],
            "targets": targets,
            "image_policy": IMAGE_POLICY,
            "verification_scope": {
                "stage": "section",
                "section_title": run.spec.sections[run.current_index],
                "require_bibliography": run.current_index == len(run.spec.sections) - 1,
            },
        }

    @staticmethod
    def figure_gaps(library, pool):
        target = pool["targets"]["new_figures"]
        if not target:
            return []
        used = set(pool["targets"]["used_figures"])
        usable = sum(item["id"] not in used for item in pool["figures"])
        if usable >= target:
            return []
        details = [f"本章需新增 {target} 张相关图，来源与文件初筛候选 {usable} 张"]
        sources = {item["id"]: item for item in library["sources"]}
        reasons = set()
        for figure in library["figures"]:
            if figure["id"] in {item["id"] for item in pool["figures"]}:
                continue
            title = sources.get(figure["source_id"], {}).get("title", "未知图源")
            reason = "未进入本章可用图源，须核对全文、相关性和文件完整性；许可状态只作提示"
            reasons.add(f"《{title}》：{reason}")
        details.extend(sorted(reasons)[:4])
        if not library["figures"]:
            details.append("当前来源尚未提取到图片，不能只复用已有文字证据满足插图目标")
        return details

    @staticmethod
    def expand_citation_markers(draft, pool):
        """只补全唯一且已有绑定的来源缩写；未知或歧义标记仍交严格校验拒绝。"""
        mapping = {}
        bound = {item.source_id for item in draft.citations}
        available = {item["id"] for item in pool["sources"]}
        for prefix in SHORT_CITE.findall(draft.content):
            matches = [identity for identity in available if identity.startswith(prefix)]
            if len(matches) != 1 or matches[0] not in bound:
                return draft, {}
            mapping[prefix] = matches[0]
        if not mapping:
            return draft, {}
        repaired = draft.model_copy(deep=True)
        repaired.content = SHORT_CITE.sub(
            lambda match: f"[[cite:{mapping[match[1]]}]]", draft.content
        )
        return repaired, mapping

    def validate(self, draft, pool):
        issues = []
        sources = {item["id"]: item for item in pool["sources"]}
        figures = {item["id"]: item for item in pool["figures"]}
        cited = set(CITE.findall(draft.content))
        inserted = FIGURE.findall(draft.content)
        if pool["targets"].get("section_role") == "contents":
            if cited or draft.citations or inserted or draft.figures:
                issues.append("目录不能包含引用或图片")
        elif cited != {item.source_id for item in draft.citations} or (
            not cited and pool["targets"]["new_sources"] > 0
        ):
            issues.append("正文引用标记与证据记录不对应，或没有真实引用")
        if (
            set(inserted) != {item.figure_id for item in draft.figures}
            or len(inserted) != len(set(inserted))
            or len(draft.figures) != len(inserted)
        ):
            issues.append("正文插图位置与图片记录不对应或重复")
        stripped = FIGURE.sub("", CITE.sub("", draft.content))
        if (
            "[[cite:" in stripped
            or "[[figure:" in stripped
            or re.search(r"https?://|!\[|<img|\]\(", stripped)
        ):
            issues.append("正文包含未受来源库约束的引用、图片或外部链接")
        paragraphs = re.split(r"\n\s*\n", draft.content)
        for index, use in enumerate(draft.citations):
            source = sources.get(use.source_id)
            if not source:
                issues.append(f"引用 {index + 1} 不在可用证据库中")
                continue
            if not any(
                claim_bound(paragraph, use.claim, f"[[cite:{use.source_id}]]")
                for paragraph in paragraphs
            ):
                issues.append(f"引用 {index + 1} 没有绑定正文同段论述")
            # 核对全文和提供给模型的具体段落，防止模型自行编造 quote 或跨页拼接。
            if not any(
                page["page"] == use.page and flat(use.quote) in flat(page["text"])
                for page in source["passages"]
            ):
                issues.append(f"引用 {index + 1} 的证据原文或页码不匹配")
        for use in draft.figures:
            figure = figures.get(use.figure_id)
            marker = f"[[figure:{use.figure_id}]]"
            if not figure:
                issues.append("图片不在当前已核对来源与文件的图片库中")
                continue
            if not re.search(r"(?m)^" + re.escape(marker) + r"\s*$", draft.content):
                issues.append("图片必须放在独立的正文位置")
            preceding = draft.content.split(marker)[0].rstrip().split("\n\n")[-1]
            if use.explanation not in preceding:
                issues.append("图片前没有对应的正文图意解释")
        targets = pool["targets"]
        if len(cited - set(targets["used_sources"])) < targets["new_sources"]:
            issues.append("当前章节实际使用的新增独立引用来源不足")
        if targets.get("section_role") == "conclusion" and (
            cited - set(targets["used_sources"]) or inserted
        ):
            issues.append("结论只能复用已使用的文献，不能引入新来源或图片")
        if len(set(inserted) - set(targets["used_figures"])) < targets["new_figures"]:
            issues.append("当前章节实际插入的新增论文图片不足")
        if issues:
            raise LocalRepairFailure(issues)

    @staticmethod
    def verdict_structure_errors(draft, verdict):
        issues = []
        if sorted(item.index for item in verdict.citations) != list(range(len(draft.citations))):
            issues.append("语义核验遗漏或重复了引用记录")
        if sorted(item.figure_id for item in verdict.figures) != sorted(
            item.figure_id for item in draft.figures
        ):
            issues.append("语义核验遗漏或重复了图片记录")
        return issues

    @staticmethod
    def verdict_errors(draft, verdict):
        issues = [*verdict.issues, *AutomaticWriting.verdict_structure_errors(draft, verdict)]
        issues += [f"证据缺口：{gap.detail}" for gap in verdict.evidence_gaps]
        issues += [
            f"引用支持性不通过：{item.reason}" for item in verdict.citations if not item.supported
        ]
        issues += [
            f"图意或图注不通过：{item.reason}"
            for item in verdict.figures
            if not item.relevant or not item.caption_accurate
        ]
        return issues

    def checked_verdict(self, state, context, draft, pool):
        # 审查漏项只重跑审查，不重新生成正文或重复下载来源。
        for _ in range(self.attempts):
            verdict = self.model.verify_section(state, context, draft, pool)
            if not draft.figures and verdict.figures:
                # 候选池或前章图片不属于当前无图章节，忽略其核验记录；正文实插图仍须逐张对应。
                verdict = verdict.model_copy(update={"figures": []})
            issues = self.verdict_structure_errors(draft, verdict)
            if not issues:
                return verdict
        raise ModelFormatError(f"写后核验结构连续 {self.attempts} 次未通过：" + "；".join(issues))

    def check_links(self, run_id, run, source_ids):
        sources = {
            item["id"]: item for item in self.store.entries(run_id, context_hash(run), "source")
        }
        fetcher = self.fetcher_factory()
        for key in source_ids:
            source = sources[key]
            try:
                retrieval.ResearchCollector(self.store, fetcher=fetcher).verify_doi(
                    Paper.model_validate(source["paper"])
                )
                final, _, raw = retrieval.recheck_known_url(
                    fetcher, source.get("pdf_url") or source["url"], limit=20_000_000
                )
                if source.get("pdf"):
                    if hashlib.sha256(raw).hexdigest() != source["pdf"]["sha256"]:
                        raise ResearchError("来源 PDF 内容已变化，不能沿用旧引文或图片")
                else:
                    if not retrieval.inspect_page(
                        raw, final, Paper.model_validate(source["paper"])
                    )[0]:
                        raise ResearchError("参考网址与原文记录不对应")
                    try:
                        body = retrieval.page_text(raw, require_body=True)
                    except retrieval.IncompleteBody:
                        final, _, raw = retrieval.recheck_known_url(fetcher, source["url"])
                        if not retrieval.inspect_page(
                            raw, final, Paper.model_validate(source["paper"])
                        )[0]:
                            raise ResearchError("重取正文后的来源元数据不对应") from None
                        body = retrieval.page_text(raw, require_body=True)
                    if body != self.pages(source)[0]["text"]:
                        raise ResearchError("来源网页正文已变化，需重新获取证据并核验")
            except ResearchError as exc:
                source.update(
                    verified=False,
                    pdf={},
                    text_asset={},
                    error=str(exc),
                    automatic_review={
                        "action": "reject",
                        "reason": str(exc),
                        "recorded_at": timestamp(),
                    },
                )
                self.store.save(run_id, context_hash(run), "source", source)
                self.store.event(
                    run_id,
                    context_hash(run),
                    "source.link_invalidated",
                    entry_id=key,
                    error=str(exc),
                )
                raise EvidenceFailure(
                    [f"来源网址或 DOI 写后核验失败：{source['title']}（{key}）：{exc}"]
                ) from None

    def render(self, run_id, run, draft, pool, *, reference_order=None):
        sources = {
            item["id"]: item for item in self.store.entries(run_id, context_hash(run), "source")
        }
        figures = {
            item["id"]: item for item in self.store.entries(run_id, context_hash(run), "figure")
        }
        previous = self.previous(run)
        order = (
            reference_order
            if reference_order is not None
            else list(
                dict.fromkeys(
                    used_references(previous)
                    + [use.source_id for use in draft.citations]
                    + [figures[use.figure_id]["source_id"] for use in draft.figures]
                )
            )
        )
        years = citation_years(run.spec, order, sources)
        image_order = list(
            dict.fromkeys(
                [key for bundle in previous for key in bundle["figure_ids"]]
                + [use.figure_id for use in draft.figures]
            )
        )
        content = CITE.sub(
            lambda match: citation_label(sources[match[1]], run.spec, order, years), draft.content
        )
        uses = {item.figure_id: item for item in draft.figures}

        def image(match):
            key = match[1]
            figure = figures[key]
            source = sources[figure["source_id"]]
            caption = uses[key].caption.replace("[", "(").replace("]", ")").replace("\n", " ")
            caption = re.sub(
                r"^(?:Figure|Fig\.?|图)\s*\d+(?:\.\d+)*[a-z]?[.:：\s-]*", "", caption, flags=re.I
            ).strip()
            number = image_order.index(key) + 1
            asset = f"/api/runs/{quote(run_id, safe='')}/research/assets/{key}"
            provenance = (
                f"Figure {figure['figure_number']}, PDF p. {figure['page']}. {figure['pdf_url']}"
                if figure.get("method") not in WEB_IMAGE_METHODS
                else f"{figure['caption']} {figure['source_url']} Image: {figure['image_url']}"
            )
            return (
                f"![Figure {number}. {caption}]({asset})\n\n"
                f"Figure {number}. {caption}\n\n"
                f"Source: {authors(source)} ({years[source['id']]}), {provenance} "
                f"License: {figure['license']['url'] or 'not confirmed'}; "
                f"status: {figure['license']['status']}."
                + (
                    " Permission or applicable terms for this image have not been confirmed."
                    if image_warnings(figure)
                    else ""
                )
                + (
                    " Copyright notice: "
                    + "; ".join(figure.get("image_review", {}).get("notices", []))
                    if figure.get("image_review", {}).get("notices")
                    else ""
                )
            )

        content = FIGURE.sub(image, content)
        if run.current_index == len(run.spec.sections) - 1 and not any(
            reference_title(title) for title in run.spec.sections
        ):
            content += "\n\n" + bibliography(run.spec, order, sources)
        return content, order

    def save_success(self, run_id, run, draft, verdict, pool, content, reference_ids, version):
        digest_value = context_hash(run)
        cited = list(dict.fromkeys(item.source_id for item in draft.citations))
        inserted = [item.figure_id for item in draft.figures]
        source_ids = set(cited) | {
            item["source_id"] for item in pool["figures"] if item["id"] in inserted
        }
        snapshots = {}
        for kind, ids in (("source", source_ids), ("figure", set(inserted))):
            for item in self.store.entries(run_id, digest_value, kind):
                if item["id"] not in ids:
                    continue
                item["automatic_review"] = {
                    "action": "accept",
                    "recorded_at": timestamp(),
                    "reason": "正文证据、网址对应与图注语义核验通过；许可状态另列提示",
                }
                self.store.save(run_id, digest_value, kind, item)
                if kind == "source":
                    snapshots[item["id"]] = {
                        "asset": item.get("pdf") or item.get("page_asset"),
                        "text_asset": item["text_asset"],
                        "url": item.get("pdf_url") or item["url"],
                        "doi": item.get("doi"),
                    }
        return {
            "version": version,
            "content_hash": digest(content),
            "context_hash": digest_value,
            "draft": draft.model_dump(),
            "verdict": verdict.model_dump(),
            "pool": {
                **pool,
                "sources": [item for item in pool["sources"] if item["id"] in source_ids],
                "figures": [item for item in pool["figures"] if item["id"] in inserted],
            },
            "citation_ids": cited,
            "figure_ids": inserted,
            "reference_ids": reference_ids,
            "sources": snapshots,
            "verified_at": timestamp(),
            "execution_plan_hash": plan_hash(run),
        }

    @staticmethod
    def inventory_counts(pool):
        targets = pool["targets"]
        sources = len({s["id"] for s in pool["sources"]} - set(targets["used_sources"]))
        figures = len({f["id"] for f in pool["figures"]} - set(targets["used_figures"]))
        missing_sources = max(0, targets["new_sources"] - sources)
        missing_figures = max(0, targets["new_figures"] - figures)
        return sources, missing_sources, missing_figures

    def inventory_gaps(self, library, pool):
        targets = pool["targets"]
        sources, missing_sources, missing_figures = self.inventory_counts(pool)
        issues = self.figure_gaps(library, pool)
        if sources < targets["new_sources"]:
            issues.insert(
                0, f"本章需新增 {targets['new_sources']} 个独立全文来源，当前有 {sources} 个"
            )
        elif not pool["sources"] and targets["new_sources"]:
            issues.insert(0, "本章缺少可核验的全文证据，当前有 0 个可用来源")
        return missing_sources, missing_figures, issues

    def remember_pool(self, run_id, digest, pool):
        self.store.save_run(
            run_id,
            digest,
            pending_pool={
                "source": [item["id"] for item in pool["sources"]],
                "figure": [item["id"] for item in pool["figures"]],
            },
        )

    def refetch_review_sources(self, run_id, digest, artifact_id, version):
        """仅按同一稿件历史证据池中的已知地址恢复被回收的来源。"""
        existing = {item["id"] for item in self.store.entries(run_id, digest, "source")}
        missing = {}
        for row in self.store.recent_writing_records(run_id, digest, artifact_id, version):
            if row.get("status") == "passed":
                break
            for source in (row.get("pool") or {}).get("sources", []):
                if source["id"] not in existing:
                    missing.setdefault(source["id"], source)
        if not missing:
            return
        collector = retrieval.ResearchCollector(
            self.store, fetcher=self.fetcher_factory(), search=object()
        )
        for source in list(missing.values())[:4]:
            pdf_url = source.get("pdf_url")
            paper = Paper(
                title=source["title"],
                authors=source["authors"],
                year=source["year"],
                doi=source.get("doi"),
                url=source["url"],
                pdf_urls=[pdf_url] if pdf_url else [],
                provider="checkpoint",
                metadata_pending=not pdf_url,
            )
            collector.refresh_source(
                run_id, digest, {**source, "paper": paper.model_dump(), "verified": False}
            )

    def prepare_evidence(
        self, run_id, state, budget, semantic_gaps, attempt, previous_pool=None, *, refreshed=None
    ):
        digest = context_hash(state)
        artifact_id = f"section-{state.current_index + 1}"
        saved = self.store.run(run_id) or {}
        same_discovery = (
            saved.get("context_hash") == digest
            and saved.get("discovery_chapter") == state.current_index + 1
        )
        focus = (
            [*saved.get("queries", []), *saved.get("web_queries", [])] if same_discovery else None
        )
        library = self.store.summary(run_id, state)
        if refreshed is None:
            refreshed = set()
        stale = [
            item
            for item in library["sources"]
            if not item.get("verified")
            and (item.get("automatic_review") or {}).get("action") == "reject"
            and (item.get("review") or {}).get("action") != "reject"
            and item["id"] not in refreshed
        ]
        if stale:
            anchors = set(self.store.passed_source_ids(run_id, digest, artifact_id))
            known = [item for item in stale if item["id"] in anchors]
            if known:
                collector = retrieval.ResearchCollector(self.store, fetcher=self.fetcher_factory())
                for item in known:
                    refreshed.add(item["id"])
                    collector.refresh_source(run_id, digest, item)
                library = self.store.summary(run_id, state)
        pool = self.pool(run_id, state, focus, summary=library)
        targets = pool["targets"]
        missing_sources, missing_figures, issues = self.inventory_gaps(library, pool)
        if not issues and not semantic_gaps:
            self.store.event(
                run_id, digest, "writing.reuse", artifact_id=artifact_id, attempt=attempt
            )
            return pool

        def available(current, kind):
            if kind == "figure":
                return {item["id"] for item in current["figures"]}
            return {
                (item["id"], passage["page"], passage["text"])
                for item in current["sources"]
                for passage in item["passages"]
            }

        # 核验意见会改变缓存段落的排序，先试上轮未送审的原文。
        baseline = previous_pool or pool

        def gaps_remaining(current):
            changed = {
                kind: bool(available(current, kind) - available(baseline, kind))
                for kind in {gap.kind for gap in semantic_gaps}
            }
            return [gap for gap in semantic_gaps if not changed[gap.kind]]

        def enough():
            current = self.pool(run_id, state, focus)
            return not any(self.inventory_counts(current)[1:]) and not gaps_remaining(current)

        def collect(queries, *, web_queries=(), figure_queries=(), candidates=(), figure_target=0):
            selection_targets = {
                **targets,
                "new_sources": missing_sources,
                "new_figures": figure_target,
                "evidence_gaps": [g.model_dump() for g in semantic_gaps],
            }
            return retrieval.ResearchCollector(
                self.store, fetcher=self.fetcher_factory(), budget=budget
            ).collect(
                run_id,
                state,
                queries,
                refresh=True,
                web_queries=web_queries,
                model_candidates=candidates,
                figure_target=figure_target,
                figure_queries=figure_queries,
                stop_when=enough,
                ranker=lambda candidates: (
                    self.model.select_sources(state, candidates, selection_targets).source_ids
                ),
            )

        unresolved = gaps_remaining(pool)
        # 先恢复已知全文或从缓存原件重新提图；这个阶段不提交任何公开搜索。
        if library["sources"] and (issues or unresolved):
            kinds = {gap.kind for gap in unresolved}
            library = collect([], figure_target=max(missing_figures, int("figure" in kinds)))
            pool = self.pool(run_id, state, focus, summary=library)
            missing_sources, missing_figures, issues = self.inventory_gaps(library, pool)
            unresolved = gaps_remaining(pool)

        if not issues and not unresolved:
            self.store.event(
                run_id, digest, "writing.reuse", artifact_id=artifact_id, attempt=attempt
            )
            return pool
        if targets["section_role"] != "conclusion" and not budget.usage()["exhausted"]:
            gaps = [
                *unresolved,
                *([EvidenceGap(kind="source", detail=issues[0])] if missing_sources else []),
                *(
                    [
                        EvidenceGap(
                            kind="figure", detail=f"尚缺 {missing_figures} 张与本章论点相关的图"
                        )
                    ]
                    if missing_figures
                    else []
                ),
            ]
            saved = self.store.run(run_id) or {}
            request = {
                **targets,
                "new_sources": missing_sources,
                "new_figures": missing_figures,
                "evidence_gaps": [gap.model_dump() for gap in gaps],
                "completed_searches": saved.get("completed_searches", [])
                if saved.get("context_hash") == digest
                and saved.get("discovery_chapter") == state.current_index + 1
                else [],
            }
            plan = self.model.research_plan(state, request)
            needs_text = any(gap.kind == "source" for gap in gaps)
            figure_target = max(missing_figures, int(any(g.kind == "figure" for g in gaps)))
            figures = (plan.figure_queries or plan.queries) if figure_target else []
            queries = plan.queries if needs_text else []
            web = plan.web_queries or queries or figures
            focus = [*queries, *web, *figures]
            semantic_gaps = unresolved
            baseline = self.pool(run_id, state, focus)
            self.store.event(
                run_id,
                digest,
                "writing.search",
                artifact_id=artifact_id,
                attempt=attempt,
                evidence_gaps=request["evidence_gaps"],
            )
            library = collect(
                queries,
                web_queries=web,
                figure_queries=figures,
                candidates=plan.source_candidates,
                figure_target=figure_target,
            )
            pool = self.pool(run_id, state, focus, summary=library)
            missing_sources, missing_figures, issues = self.inventory_gaps(library, pool)
            unresolved = gaps_remaining(pool)

        if issues or unresolved:
            self.remember_pool(run_id, digest, pool)
            if state.outline_plan:
                needs = state.outline_plan.parts[state.current_index].evidence_needs
                if needs:
                    issues.append("本章大纲要求的证据：" + "、".join(needs))
            raise MissingEvidence(
                [
                    "当前证据不足以完成本章",
                    *issues,
                    *(g.detail for g in unresolved),
                    *library["issues"][:4],
                ],
                unresolved,
            )
        return pool

    def repair_draft(self, run_id, state, draft, pool, version, attempt):
        digest = context_hash(state)
        artifact_id = f"section-{state.current_index + 1}"
        try:
            self.validate(draft, pool)
        except LocalRepairFailure as exc:
            matches = [
                re.fullmatch(r"引用 (\d+) 的证据原文或页码不匹配", issue) for issue in exc.issues
            ]
            if matches and all(matches):
                indices = [int(m[1]) - 1 for m in matches]
                self.store.event(
                    run_id,
                    digest,
                    "citation.repair_attempt",
                    artifact_id=artifact_id,
                    indices=indices,
                )
                try:
                    patches = self.model.repair_citations(state, draft, pool, indices).repairs
                except ModelFormatError as error:
                    # 已返回的无效补丁可以回退到同证据池的绑定修复，连接错误仍保留断点。
                    self.store.event(
                        run_id,
                        digest,
                        "citation.repair_fallback",
                        artifact_id=artifact_id,
                        issues=[str(error)],
                    )
                    patches = []
                if sorted(p.index for p in patches) != sorted(indices):
                    if patches:
                        raise LocalRepairFailure(
                            ["引用绑定修复遗漏、重复或超出了待修复记录"]
                        ) from None
                    repaired = self.model.repair_bindings(state, draft, pool, exc.issues)
                else:
                    repaired = draft.model_copy(deep=True)
                    for patch in patches:
                        repaired.citations[patch.index].quote = patch.quote
                        repaired.citations[patch.index].page = patch.page
            else:
                repaired = self.model.repair_bindings(state, draft, pool, exc.issues)
            if unmarked_paragraphs(repaired.content) != unmarked_paragraphs(draft.content):
                raise LocalRepairFailure(
                    [*exc.issues, "绑定修复修改了正文措辞，已拒绝该补丁"]
                ) from None
            try:
                self.validate(repaired, pool)
            except LocalRepairFailure as repair_error:
                self.store.writing_event(
                    run_id,
                    digest,
                    artifact_id,
                    version,
                    attempt=attempt,
                    status="bindings_rejected",
                    draft=repaired.model_dump(),
                    pool=pool,
                    issues=repair_error.issues,
                )
                raise
            draft = repaired
            self.store.writing_event(
                run_id,
                digest,
                artifact_id,
                version,
                attempt=attempt,
                status="bindings_repaired",
                draft=draft.model_dump(),
                pool=pool,
            )
        return draft

    def write(self, run_id, run, context, version):
        restore_revisions(self.store, run_id, run)
        artifact_id = f"section-{run.current_index + 1}"
        run.automatic_research = True
        if reference_title(run.spec.sections[run.current_index]):
            return self.write_references(run_id, run, version)
        if contents_title(run.spec.sections[run.current_index]):
            return self.write_contents(run_id, run, version)
        budget = SearchBudget(
            self.store,
            run_id,
            run.current_index + 1,
            allow_chapter_overflow=self.targets(run)["source_topup"],
        )
        feedback = run.feedback
        # 恢复与轮内重试都累计本版失败意见，避免后续格式错误覆盖已经发现的语义限制。
        last_issues = self.store.last_writing_issues(
            run_id, context_hash(run), artifact_id, version
        )
        saved_draft = self.store.resumable_draft(run_id, context_hash(run), artifact_id, version)
        draft = EvidenceDraft.model_validate(saved_draft) if saved_draft else None
        # 旧断点的缺图意见属于已撤销的模型建议，不应诱导恢复稿继续插入无关图片。
        if (
            draft is not None
            and not draft.figures
            and not FIGURE.search(draft.content)
            and self.targets(run)["image_goal_optional"]
        ):
            stale_image_issues = {
                "当前章节实际插入的新增论文图片不足",
                "图片前没有对应的正文图意解释",
                "绑定修复修改了正文措辞，已拒绝该补丁",
            }
            last_issues = [issue for issue in last_issues if issue not in stale_image_issues]
        repair_issues = list(last_issues)
        saved_gaps, previous_pool = (
            self.store.pending_evidence_review(
                run_id,
                context_hash(run),
                artifact_id,
                version,
                minimum_policy=EVIDENCE_POLICY_VERSION if budget.usage()["exhausted"] else None,
            )
            if draft is None
            else ([], None)
        )
        if not saved_gaps and previous_pool is not None:
            self.refetch_review_sources(run_id, context_hash(run), artifact_id, version)
        semantic_gaps = [EvidenceGap.model_validate(item) for item in saved_gaps]
        cached_gap_recheck = True
        refreshed = set()
        needs_evidence = False
        for attempt in range(1, self.attempts + 1):
            state = run.model_copy(deep=True)
            state.feedback = feedback + (
                "\n此前失败稿的核验修复意见（旧引用编号仅定位旧稿，不对应本次新稿；"
                "按原材料逐项自检，保留已修正的事实范围）：" + "；".join(repair_issues)
                if repair_issues
                else ""
            )
            pool = None
            try:
                # 最后一轮有明确缺口时保留定向检索机会，仍可先按已知网址重新取证。
                recheck_pool = (
                    previous_pool if cached_gap_recheck and attempt < self.attempts else None
                )
                if recheck_pool is not None:
                    cached_gap_recheck = False
                pool = self.prepare_evidence(
                    run_id, state, budget, semantic_gaps, attempt, recheck_pool, refreshed=refreshed
                )
                self.remember_pool(run_id, context_hash(run), pool)
                semantic_gaps = []
                previous_pool = None
                if draft is None:
                    OutlineReviser(self.store, self.model).adapt(
                        run_id,
                        state,
                        context,
                        pool,
                        version,
                        lambda ids: self.check_links(run_id, run, ids),
                    )
                    if state.outline_plan:
                        run.outline_plan = state.outline_plan.model_copy(deep=True)
                    draft = self.model.section_with_evidence(state, context, pool)
                    self.store.writing_event(
                        run_id,
                        context_hash(run),
                        artifact_id,
                        version,
                        attempt=attempt,
                        status="drafted",
                        draft=draft.model_dump(),
                        pool=pool,
                    )
                expanded, marker_mapping = self.expand_citation_markers(draft, pool)
                if marker_mapping:
                    draft = expanded
                    self.store.writing_event(
                        run_id,
                        context_hash(run),
                        artifact_id,
                        version,
                        attempt=attempt,
                        status="markers_expanded",
                        draft=draft.model_dump(),
                        pool=pool,
                        mapping=marker_mapping,
                    )
                    self.store.event(
                        run_id,
                        context_hash(run),
                        "citation.markers_expanded",
                        artifact_id=artifact_id,
                        mapping=marker_mapping,
                    )
                draft = self.repair_draft(run_id, state, draft, pool, version, attempt)
                figure_ids = {use.figure_id for use in draft.figures}
                source_ids = {use.source_id for use in draft.citations} | {
                    item["source_id"] for item in pool["figures"] if item["id"] in figure_ids
                }
                self.check_links(run_id, run, source_ids)
                self.check_figure_links(run_id, run, figure_ids)
                latest_figures = {
                    f["id"]: f for f in self.store.entries(run_id, context_hash(run), "figure")
                }
                for figure in pool["figures"]:
                    if figure["id"] in figure_ids:
                        figure["license"] = latest_figures[figure["id"]]["license"]
                        figure["license_warnings"] = image_warnings(latest_figures[figure["id"]])
                content, order = self.render(run_id, run, draft, pool)
                pool["inserted_article"] = content
                verdict = self.checked_verdict(state, context, draft, pool)
                errors = self.verdict_errors(draft, verdict)
                if verdict.evidence_gaps and pool["targets"]["section_role"] != "conclusion":
                    raise MissingEvidence(errors, verdict.evidence_gaps)
                if errors:
                    raise EvidenceFailure(errors)
                for item in verdict.figures:
                    figure = latest_figures[item.figure_id]
                    figure["license_review"] = {
                        "applicable": item.license_applicable,
                        "reason": item.reason,
                    }
                    figure["license_warnings"] = image_warnings(figure)
                    self.store.save(run_id, context_hash(run), "figure", figure)
                content, order = self.render(run_id, run, draft, pool)
                pool["inserted_article"] = content
                bundle = self.save_success(
                    run_id, run, draft, verdict, pool, content, order, version
                )
                self.store.writing_event(
                    run_id,
                    context_hash(run),
                    artifact_id,
                    version,
                    attempt=attempt,
                    status="passed",
                    content=content,
                    evidence=bundle,
                )
                self.store.event(
                    run_id,
                    context_hash(run),
                    "writing.verified",
                    artifact_id=artifact_id,
                    attempt=attempt,
                    citations=len(draft.citations),
                    figures=len(draft.figures),
                )
                self.store.save_run(
                    run_id, context_hash(run), status="writing", issues=[], pending_pool={}
                )
                self.store.cleanup_candidates(run_id, context_hash(run))
                return content, bundle
            except (ResearchError, ModelError) as exc:
                local = isinstance(exc, (LocalRepairFailure, ModelError))
                if not local:
                    draft = None
                needs_evidence = isinstance(exc, MissingEvidence)
                if needs_evidence:
                    semantic_gaps = exc.gaps
                    if pool is not None:
                        previous_pool = pool
                last_issues = exc.issues if isinstance(exc, EvidenceFailure) else [str(exc)]
                repair_issues = list(dict.fromkeys([*repair_issues, *last_issues]))
                self.store.writing_event(
                    run_id,
                    context_hash(run),
                    artifact_id,
                    version,
                    attempt=attempt,
                    status="failed",
                    next_action="repair" if local else "rewrite",
                    evidence_policy=EVIDENCE_POLICY_VERSION,
                    issues=last_issues,
                    evidence_gaps=[gap.model_dump() for gap in exc.gaps] if needs_evidence else [],
                )
                self.store.event(
                    run_id,
                    context_hash(run),
                    "writing.retry",
                    artifact_id=artifact_id,
                    attempt=attempt,
                    action="repair" if local else "search" if needs_evidence else "rewrite",
                    issues=last_issues,
                )
                # API 连接/鉴权失败交给原检查点恢复，不重复消耗模型请求。
                if isinstance(exc, ModelError) and not isinstance(exc, ModelFormatError):
                    self.store.save_run(
                        run_id, context_hash(run), status="interrupted", issues=last_issues
                    )
                    self.store.cleanup_candidates(run_id, context_hash(run), preserve_pending=True)
                    raise
                if isinstance(exc, ModelFormatError):
                    break
                if needs_evidence and budget.usage()["exhausted"]:
                    break
        usage = budget.usage()
        exhausted = needs_evidence and usage["exhausted"]
        if exhausted:
            title = run.spec.sections[run.current_index]
            last_issues = list(
                dict.fromkeys(
                    [
                        budget.notice(usage),
                        f"已保存第 {usage['chapter']} 章《{title}》的写作断点",
                        *last_issues,
                        *repair_issues,
                    ]
                )
            )[:20]
        self.store.save_run(
            run_id,
            context_hash(run),
            status="search_budget_exhausted" if exhausted else "partial",
            issues=last_issues,
        )
        self.store.cleanup_candidates(run_id, context_hash(run), preserve_pending=True)
        if exhausted:
            raise EvidenceFailure(last_issues)
        raise EvidenceFailure(
            [
                f"写作或修复连续 {attempt} 次未通过，已保留记录，可恢复任务",
                *last_issues,
            ]
        )

    def write_contents(self, run_id, run, version):
        """目录只列已确认的后续章节，不借用学术来源或消耗搜索额度。"""
        content = contents_text(run)
        draft = EvidenceDraft(content=content)
        pool = {"targets": self.targets(run), "sources": [], "figures": []}
        self.validate(draft, pool)
        verdict = EvidenceVerdict(citations=[], figures=[])
        bundle = self.save_success(run_id, run, draft, verdict, pool, content, [], version)
        bundle["contents_section"] = True
        self.store.writing_event(
            run_id,
            context_hash(run),
            f"section-{run.current_index + 1}",
            version,
            status="passed",
            content=content,
            evidence=bundle,
        )
        self.store.save_run(run_id, context_hash(run), status="writing", issues=[], pending_pool={})
        self.store.cleanup_candidates(run_id, context_hash(run))
        return content, bundle

    def write_references(self, run_id, run, version):
        previous = self.previous(run)
        order = used_references(previous)
        sources = {
            item["id"]: item for item in self.store.entries(run_id, context_hash(run), "source")
        }
        if not order:
            raise EvidenceFailure(["正文尚无已核验引用，不能生成参考文献列表"])
        self.check_links(run_id, run, order)
        self.enrich_citation_metadata(run_id, run, order, sources)
        content = bibliography(run.spec, order, sources, heading=False)
        bundle = {
            "version": version,
            "content_hash": digest(content),
            "context_hash": context_hash(run),
            "citation_ids": [],
            "figure_ids": [],
            "reference_ids": order,
            "sources": {},
            "verified_at": timestamp(),
            "reference_section": True,
        }
        self.store.writing_event(
            run_id,
            context_hash(run),
            f"section-{run.current_index + 1}",
            version,
            status="passed",
            content=content,
            evidence=bundle,
        )
        return content, bundle

    def enrich_citation_metadata(self, run_id, run, order, sources):
        """为已核验 DOI 补存书目字段；该查询不消耗文献搜索次数。"""
        if not re.search(r"\bapa\b", run.spec.citation_style or "", re.I):
            return
        collector = retrieval.ResearchCollector(self.store, fetcher=self.fetcher_factory())
        for key in order:
            source = sources[key]
            changed = False
            if not source.get("citation_metadata") and source.get("doi"):
                try:
                    metadata = collector.verify_doi(Paper.model_validate(source["paper"]))
                except ResearchError:
                    metadata = None
                if metadata:
                    source["citation_metadata"] = metadata
                    changed = True
            if retrieval.reconcile_pdf_authors(source, self.store):
                changed = True
            if changed:
                self.store.save(run_id, context_hash(run), "source", source)

    @staticmethod
    def reference_fingerprint(run, index, bundle, sources, figures):
        figure_fields = (
            "sha256",
            "source_id",
            "method",
            "page",
            "figure_number",
            "caption",
            "pdf_url",
            "source_url",
            "display_url",
            "image_url",
            "original_sha256",
            "provenance_asset",
            "license",
        )
        # 编号和参考文献排序由原始标记重算；复核结果只绑定本章实际论述和出处。
        data = {
            "policy": EVIDENCE_POLICY_VERSION,
            "plan": plan_hash(run, index),
            "draft": bundle["draft"],
            "sources": bundle["sources"],
            "papers": {key: sources[key]["paper"] for key in bundle["sources"]},
            "figures": {
                key: {field: figures[key].get(field) for field in figure_fields}
                for key in bundle["figure_ids"]
            },
        }
        return digest(json.dumps(data, sort_keys=True))

    def audit(self, run_id, run, context):
        """按实际使用的来源统一渲染全文引用，再核验本次交付。"""
        revisions = restore_revisions(self.store, run_id, run)
        revised = {r["part_index"] for r in revisions if r["status"] == "outline_revised"}
        source_ids = set()
        image_ids = set()
        reference_ids = used_references(
            run.grounded_drafts[item.artifact_id]
            for item in run.drafts
            if item.artifact_id in run.grounded_drafts
        )
        run_context = context_hash(run)
        summary = self.store.summary(run_id, run)
        sources = {item["id"]: item for item in summary["sources"]}
        self.enrich_citation_metadata(run_id, run, reference_ids, sources)
        figures = {item["id"]: item for item in summary["figures"]}
        checks = self.store.reference_checks(run_id, run_context)
        reference_list = bibliography(run.spec, reference_ids, sources)
        format_updates = []
        for index, artifact in enumerate(run.drafts):
            bundle = run.grounded_drafts.get(artifact.artifact_id)
            if (
                not bundle
                or bundle["version"] != artifact.version
                or bundle["content_hash"] != digest(artifact.content)
                or bundle["context_hash"] != run_context
                or (index in revised and bundle.get("execution_plan_hash") != plan_hash(run, index))
            ):
                return self.audit_failure(
                    run_id, run, index, ["当前章节缺少与本版正文一致的自动引用核验记录"]
                )
            source_ids.update(bundle["citation_ids"])
            image_ids.update(bundle["figure_ids"])
            if contents_title(run.spec.sections[index]):
                try:
                    draft = EvidenceDraft.model_validate(bundle["draft"])
                    self.validate(draft, bundle["pool"])
                    state = run.model_copy(update={"current_index": index})
                    if (
                        not bundle.get("contents_section")
                        or artifact.content != contents_text(state)
                        or draft.content != artifact.content
                    ):
                        raise EvidenceFailure(["目录与当前章节顺序不一致，需重新生成"])
                except (KeyError, ValueError, EvidenceFailure) as exc:
                    issues = (
                        exc.issues if isinstance(exc, EvidenceFailure) else ["目录核验记录无效"]
                    )
                    return self.audit_failure(run_id, run, index, issues)
                continue
            if bundle.get("reference_section"):
                content = bibliography(run.spec, reference_ids, sources, heading=False)
                if content != artifact.content:
                    format_updates.append(
                        (index, content, {**bundle, "reference_ids": reference_ids})
                    )
                continue
            try:
                draft = EvidenceDraft.model_validate(bundle["draft"])
                self.validate(draft, bundle["pool"])
                for key, snapshot in bundle["sources"].items():
                    current = sources.get(key, {})
                    if (
                        not current.get("integrity_ok")
                        or (current.get("pdf") or current.get("page_asset")) != snapshot["asset"]
                        or current.get("text_asset") != snapshot["text_asset"]
                        or (current.get("pdf_url") or current.get("url")) != snapshot["url"]
                        or current.get("doi") != snapshot["doi"]
                        or not self.store.asset_valid(snapshot["text_asset"])
                    ):
                        raise EvidenceFailure(
                            ["章节引用的来源快照已更新，需按新全文重新写作与核验"]
                        )
                if not all(
                    figures.get(key, {}).get("integrity_ok") for key in bundle["figure_ids"]
                ):
                    raise EvidenceFailure(["正文插图文件已损坏或来源 PDF 已变化"])
                fingerprint = self.reference_fingerprint(run, index, bundle, sources, figures)
                verdict = checks.get((artifact.artifact_id, artifact.version, fingerprint))
                if verdict is None:
                    self.check_links(run_id, run, bundle["sources"])
                    self.check_figure_links(run_id, run, bundle["figure_ids"])
                state = run.model_copy(update={"current_index": index})
                content, _ = self.render(
                    run_id, state, draft, bundle["pool"], reference_order=reference_ids
                )
                if verdict is None:
                    pool = {
                        **bundle["pool"],
                        "sources": [
                            {
                                **item,
                                "authors": sources[item["id"]]["authors"],
                                "citation_author": authors(sources[item["id"]]),
                            }
                            if item["id"] in sources
                            else item
                            for item in bundle["pool"]["sources"]
                        ],
                        "inserted_article": content,
                        "verification_scope": {
                            "stage": "final_reference",
                            "section_title": run.spec.sections[index],
                            "require_bibliography": index == len(run.spec.sections) - 1
                            and not any(reference_title(title) for title in run.spec.sections),
                        },
                        "reference_list": reference_list,
                    }
                    reviewed = self.checked_verdict(state, context, draft, pool)
                    if issues := self.verdict_errors(draft, reviewed):
                        raise EvidenceFailure(issues)
                    verdict = reviewed.model_dump()
                    self.store.writing_event(
                        run_id,
                        run_context,
                        artifact.artifact_id,
                        artifact.version,
                        status="reference_checked",
                        fingerprint=fingerprint,
                        verdict=verdict,
                    )
                if content != artifact.content:
                    format_updates.append(
                        (
                            index,
                            content,
                            {
                                **bundle,
                                "pool": {**bundle["pool"], "inserted_article": content},
                                "verdict": verdict,
                            },
                        )
                    )
            except EvidenceFailure as exc:
                return self.audit_failure(run_id, run, index, exc.issues)
            except ModelFormatError as exc:
                self.store.save_run(
                    run_id, context_hash(run), status="interrupted", issues=[str(exc)]
                )
                raise
        if (
            len(source_ids) < run.spec.min_sources
            or len(image_ids) < required_web_images(run)
            or not summary["ready"]
        ):
            # 全文来源差额回到最后一个论证章节补足，不让目录或封面承担引用。
            source_chapters = body_indices(run.spec.sections)
            repair_index = (
                source_chapters[-1]
                if len(source_ids) < run.spec.min_sources and source_chapters
                else 0
            )
            return self.audit_failure(
                run_id,
                run,
                repair_index,
                ["全文实际引用与插图尚未满足独立来源和不同图片目标，重新规划证据并写作"],
            )
        for index, content, bundle in format_updates:
            old = run.drafts[index]
            artifact = old.model_copy(update={"version": old.version + 1, "content": content})
            bundle.update(
                version=artifact.version, content_hash=digest(content), verified_at=timestamp()
            )
            self.store.writing_event(
                run_id,
                context_hash(run),
                artifact.artifact_id,
                artifact.version,
                status="passed",
                content=content,
                evidence=bundle,
            )
            run.drafts[index] = artifact
            run.grounded_drafts[artifact.artifact_id] = bundle
            run.history.append(artifact)
            run.approvals.append(
                Approval(
                    interrupt_id=f"auto:{artifact.artifact_id}:{artifact.version}",
                    artifact_id=artifact.artifact_id,
                    version=artifact.version,
                    action="approve",
                    feedback="全文核验通过后统一排版引用和参考文献。",
                )
            )
            self.store.event(
                run_id,
                context_hash(run),
                "reference.labels_updated",
                artifact_id=artifact.artifact_id,
                version=artifact.version,
            )
        self.store.event(
            run_id,
            context_hash(run),
            "reference.passed",
            sources=len(source_ids),
            figures=len(image_ids),
        )
        self.store.save_run(run_id, context_hash(run), status="verified", issues=[])
        return None, []

    def audit_failure(self, run_id, run, index, issues):
        self.audit_repairs += 1
        self.store.event(
            run_id,
            context_hash(run),
            "reference.failed",
            artifact_id=f"section-{index + 1}",
            issues=issues,
            automatic_repair=self.audit_repairs,
        )
        if getattr(self.model, "autonomous", False) and self.audit_repairs >= 3:
            raise EvidenceFailure(["最终核验连续三轮未通过，已保存记录，可从检查点恢复", *issues])
        return index, issues

    def check_figure_links(self, run_id, run, ids):
        fetcher = self.fetcher_factory()
        pages = {}
        sources = {s["id"]: s for s in self.store.entries(run_id, context_hash(run), "source")}
        for figure in self.store.entries(run_id, context_hash(run), "figure"):
            if figure["id"] not in ids:
                continue
            if not self.review_image(run_id, run, figure):
                raise EvidenceFailure(["图片文件缺失或无法解码，需重新获取"])
            if figure.get("method") not in WEB_IMAGE_METHODS:
                continue
            if figure.get("method") == "web_page_image":
                if not self.store.asset_valid(figure.get("provenance_asset")):
                    raise EvidenceFailure(["网页图片的来源关联证据缺失或变化"])
                proof = json.loads(
                    (self.store.assets / figure["provenance_asset"]["filename"]).read_bytes()
                )
                if (
                    any(
                        proof.get(k) != figure.get(k)
                        for k in ("image_url", "original_sha256", "caption")
                    )
                    or proof.get("page_sha256") != figure.get("page_sha256")
                    or proof.get("page_url") != figure.get("source_url")
                ):
                    raise EvidenceFailure(["网页图片与保存的来源关联证据不对应"])
            if figure["source_url"] not in pages:
                final, _, raw = fetcher.get(figure["source_url"])
                try:
                    retrieval.page_text(raw, require_body=True)
                except retrieval.IncompleteBody:
                    final, _, raw = fetcher.get(figure["source_url"])
                    retrieval.page_text(raw, require_body=True)
                pages[figure["source_url"]] = page_image_links(raw, final)
            expected = figure.get("display_url") or figure["image_url"]
            if expected not in pages[figure["source_url"]]:
                self.store.save(run_id, context_hash(run), "figure", {**figure, "retired": True})
                self.store.save(
                    run_id,
                    context_hash(run),
                    "source",
                    {**sources[figure["source_id"]], "verified": False},
                )
                raise EvidenceFailure(["原网页不再关联该图片，需重新检索"])
            _, _, raw = fetcher.get(figure["image_url"], limit=8_000_000)
            if hashlib.sha256(raw).hexdigest() != figure["original_sha256"]:
                self.store.save(run_id, context_hash(run), "figure", {**figure, "retired": True})
                source = next(
                    item
                    for item in self.store.entries(run_id, context_hash(run), "source")
                    if item["id"] == figure["source_id"]
                )
                self.store.save(run_id, context_hash(run), "source", {**source, "verified": False})
                raise EvidenceFailure(["网络原图内容已变化，需重新检索并核验"])

    def review_image(self, run_id, run, figure):
        if not self.store.asset_valid(figure):
            return False
        checks = figure.get("image_review") or {}
        if checks.get("version") != IMAGE_REVIEW_VERSION:
            try:
                checks = inspect_image_notice((self.store.assets / figure["filename"]).read_bytes())
            except ResearchError:
                try:
                    with Image.open(self.store.assets / figure["filename"]) as image:
                        image.verify()
                except (OSError, ValueError, Image.DecompressionBombError):
                    return False
                checks = {
                    "version": IMAGE_REVIEW_VERSION,
                    "status": "unavailable",
                    "notices": ["本地版权 OCR 未完成，未确认图片许可。"],
                }
            figure["image_review"] = checks
        if checks["status"] == "blocked":
            figure["license"] = {
                **figure["license"],
                "status": "needs_review",
                "evidence": "图片内声明与通用许可需分别核对：" + "；".join(checks["notices"]),
            }
        figure["license_warnings"] = image_warnings(figure)
        self.store.save(run_id, context_hash(run), "figure", figure)
        return checks["status"] in {"checked", "blocked", "unavailable"}

    def assert_exportable(self, run_id, run):
        """下载不再调用模型；严格检查确认版本、来源快照及所有本地插图。"""
        if not run.automatic_research:
            return
        revisions = restore_revisions(self.store, run_id, run)
        revised = {r["part_index"] for r in revisions if r["status"] == "outline_revised"}
        summary = self.store.summary(run_id, run)
        if not summary["ready"]:
            raise EvidenceFailure(["引用或插图文件不完整，不能导出；请恢复自动核验"])
        for index, artifact in enumerate(run.drafts):
            bundle = run.grounded_drafts.get(artifact.artifact_id)
            if (
                not bundle
                or bundle["version"] != artifact.version
                or bundle["content_hash"] != digest(artifact.content)
                or (index in revised and bundle.get("execution_plan_hash") != plan_hash(run, index))
            ):
                raise EvidenceFailure(["正文版本与引用核验结果不一致，不能导出"])
            valid_figures = {item["id"] for item in summary["figures"] if item["integrity_ok"]}
            if not set(bundle["figure_ids"]).issubset(valid_figures):
                raise EvidenceFailure(["正文中的已核验图片缺失，不能导出"])
            for key, snapshot in bundle["sources"].items():
                current = next((item for item in summary["sources"] if item["id"] == key), None)
                if (
                    not current
                    or (current.get("pdf") or current.get("page_asset")) != snapshot["asset"]
                    or current.get("text_asset") != snapshot["text_asset"]
                    or (current.get("pdf_url") or current["url"]) != snapshot["url"]
                    or current.get("doi") != snapshot["doi"]
                ):
                    raise EvidenceFailure(["参考来源记录与本版正文的核验快照不一致，不能导出"])
                if not self.store.asset_valid(snapshot["asset"]) or not self.store.asset_valid(
                    snapshot["text_asset"]
                ):
                    raise EvidenceFailure(["参考来源缓存变化，不能沿用旧核验结果导出"])

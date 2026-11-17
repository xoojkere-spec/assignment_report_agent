"""保存 skill 文档快照，并按阶段构建最小规则上下文。"""

from hashlib import sha256
from pathlib import Path

from assignment_agent.schemas import RunState, SkillDocument

DEFAULT_SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills" / "undergraduate-assignment"
BASE = ["SKILL.md", "references/using-research-writing/guide.md"]
STAGES = {
    "parse": ["brainstorming-research"],
    "outline": ["paper-orchestration"],
    "section": ["writing-chapters", "writing-core"],
    "finish": ["peer-review", "verification"],
}


def stage_paths(stage: str, evidence: bool = False) -> list[str]:
    if stage not in STAGES:
        raise ValueError(f"未知 skill 阶段：{stage}")
    modules = list(STAGES[stage])
    if evidence and stage == "section":
        modules += ["evidence-driven-writing", "literature-review"]
    return BASE + [f"references/{module}/guide.md" for module in modules]


def snapshot_skill(root: str | Path, destination: Path) -> tuple[str, dict[str, str]]:
    root = Path(root).resolve(strict=True)
    if "name: undergraduate-assignment" not in (root / "SKILL.md").read_text(encoding="utf-8"):
        raise ValueError("必须使用 undergraduate-assignment skill")
    files = {}
    # 快照仅复制文档，不执行 skill 中的脚本；阶段上下文仍按需加载。
    for path in sorted(root.rglob("*.md")):
        if not path.resolve().is_relative_to(root):
            raise ValueError("skill 文档路径越出根目录")
        files[path.relative_to(root).as_posix()] = path.read_bytes()
    required = {path for stage in STAGES for path in stage_paths(stage, evidence=True)}
    if missing := required - files.keys():
        raise ValueError(f"skill 缺少必要模块：{sorted(missing)}")
    manifest = {name: sha256(raw).hexdigest() for name, raw in files.items()}
    digest = sha256(repr(sorted(manifest.items())).encode()).hexdigest()
    target = destination / digest
    for name, raw in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError(f"skill 快照已被修改：{name}")
        else:
            path.write_bytes(raw)
    return str(target.resolve()), manifest


def load_stage(run: RunState, stage: str) -> None:
    if run.skill_root is None:
        run.active_skill_paths = []
        return
    # 附带 rubric 不等于引用文献；仅有课程材料时加入证据模块。
    paths = stage_paths(stage, evidence=any(item.role == "course" for item in run.materials))
    for name in paths:
        if name in run.skill_documents:
            continue
        path = (Path(run.skill_root) / name).resolve(strict=True)
        if not path.is_relative_to(Path(run.skill_root).resolve()):
            raise ValueError("skill 模块路径越出快照目录")
        raw = path.read_bytes()
        digest = sha256(raw).hexdigest()
        if digest != run.skill_manifest.get(name):
            raise ValueError(f"skill 快照校验失败：{name}")
        run.skill_documents[name] = SkillDocument(
            path=name, sha256=digest, content=raw.decode("utf-8-sig")
        )
    run.active_skill_paths = paths


def active_context(run: RunState) -> dict[str, str]:
    return {name: run.skill_documents[name].content for name in run.active_skill_paths}

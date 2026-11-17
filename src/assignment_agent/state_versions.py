"""只升级已知任务格式；恢复路径在读取时映射，不覆盖历史检查点。"""

import copy
import json
from pathlib import Path


def upgrade_state(value):
    if not isinstance(value, dict):
        return value
    version = value.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise ValueError("任务状态版本不受当前程序支持")
    return {**value, "schema_version": 1}


def path_mappings(database):
    database = Path(database).resolve()
    sidecar = database.with_suffix(database.suffix + ".paths.json")
    if not sidecar.exists():
        return []
    values = json.loads(sidecar.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError("恢复路径清单格式无效")
    for value in values:
        if not isinstance(value, dict) or set(value) != {"original", "restored"}:
            raise ValueError("恢复路径清单格式无效")
        if not Path(value["original"]).is_absolute() or not Path(value["restored"]).is_absolute():
            raise ValueError("恢复路径必须是绝对路径")
        if not Path(value["restored"]).resolve().is_relative_to(database.parent):
            raise ValueError("恢复路径越出副本目录")
    return values


def relocate_state(value, database):
    mappings = path_mappings(database)
    if not mappings or not isinstance(value, dict):
        return value

    def relocated(raw):
        path = Path(raw).resolve()
        if any(path.is_relative_to(Path(m["restored"])) for m in mappings):
            return str(path)
        matches = [m for m in mappings if path.is_relative_to(Path(m["original"]))]
        if not matches:
            raise ValueError("任务文件路径未包含在恢复清单中")
        match = max(matches, key=lambda m: len(m["original"]))
        return str(Path(match["restored"]) / path.relative_to(match["original"]))

    result = copy.deepcopy(value)
    if result.get("skill_root"):
        result["skill_root"] = relocated(result["skill_root"])
    for material in result.get("materials", []):
        material["path"] = relocated(material["path"])
    return result


def load_run(value, database):
    from assignment_agent.schemas import RunState

    return RunState.model_validate(relocate_state(value, database))

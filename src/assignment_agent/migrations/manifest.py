"""恢复前验证清单结构、目标布局和数据库必需文件，验证过程不创建恢复目录。"""

import re
from pathlib import Path

from assignment_agent.migrations.assets import required_assets
from assignment_agent.migrations.inspection import MigrationError


def relative_path(value, *, root=False):
    if not isinstance(value, str) or not value:
        raise MigrationError("备份清单相对路径无效")
    path = Path(value)
    if path.anchor or ".." in path.parts or (path == Path(".") and not root):
        raise MigrationError("备份清单包含越界路径")
    return path


def absolute_path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise MigrationError("备份清单原路径必须是绝对路径")
    return Path(value).resolve()


def valid_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise MigrationError("备份清单哈希格式无效")
    return value


def mapped_path(roots, original):
    original = Path(original).resolve()
    matches = [path for path in roots if original.is_relative_to(path)]
    if not matches:
        raise MigrationError("必需文件未包含在恢复路径映射中")
    selected = max(matches, key=lambda path: len(path.parts))
    return roots[selected] / original.relative_to(selected)


def validate_layout(manifest):
    if not isinstance(manifest, dict) or set(manifest) != {
        "format",
        "database_name",
        "database_sha256",
        "roots",
        "files",
    }:
        raise MigrationError("备份清单字段缺失或不受支持")
    if type(manifest["format"]) is not int or manifest["format"] != 1:
        raise MigrationError("备份清单版本不受支持")
    name = relative_path(manifest["database_name"])
    if len(name.parts) != 1:
        raise MigrationError("备份数据库名称无效")
    valid_hash(manifest["database_sha256"])
    reserved = {name, Path(str(name) + ".paths.json")}
    if not isinstance(manifest["roots"], list) or not manifest["roots"]:
        raise MigrationError("恢复路径映射必须是非空列表")
    roots = {}
    for item in manifest["roots"]:
        if not isinstance(item, dict) or set(item) != {"original", "relative"}:
            raise MigrationError("恢复路径映射格式无效")
        original = absolute_path(item["original"])
        relative = relative_path(item["relative"], root=True)
        if any(path == relative or path in relative.parents for path in reserved):
            raise MigrationError("恢复路径与数据库目标冲突")
        if original in roots and roots[original] != relative:
            raise MigrationError("恢复路径映射存在冲突")
        roots[original] = relative
    if Path(".") not in roots.values():
        raise MigrationError("恢复路径映射缺少数据库根目录")
    if not isinstance(manifest["files"], list):
        raise MigrationError("备份文件清单必须是列表")
    files, originals = {}, set()
    for item in manifest["files"]:
        if not isinstance(item, dict):
            raise MigrationError("备份文件条目格式无效")
        relative = relative_path(item.get("relative"))
        if not {"original", "relative", "sha256"} <= item.keys() or item.keys() - {
            "original",
            "relative",
            "sha256",
            "stored",
        }:
            raise MigrationError("备份文件条目字段无效")
        original = absolute_path(item["original"])
        valid_hash(item["sha256"])
        relative_path(item.get("stored", "files/" + item["relative"]))
        if relative in files or original in originals:
            raise MigrationError("备份文件清单包含重复条目")
        if any(path == relative or path in relative.parents for path in reserved):
            raise MigrationError("恢复文件与数据库目标冲突")
        if mapped_path(roots, original) != relative:
            raise MigrationError("文件位置与恢复路径映射不一致")
        files[relative] = item
        originals.add(original)
    for path in files:
        if any(parent in files for parent in path.parents):
            raise MigrationError("恢复文件与目录布局冲突")
    return roots, files


def validate_required_files(connection, states, roots, files):
    expected = {}

    def require(path, sha):
        sha = valid_hash(sha)
        if path in expected and expected[path] != sha:
            raise MigrationError("必需文件的历史哈希存在冲突")
        expected[path] = sha
        if path not in files or files[path]["sha256"] != sha:
            raise MigrationError("备份清单缺少必需文件或其引用哈希不符")

    for name, sha in required_assets(connection, states).items():
        require(Path("research-assets") / name, sha)
    for state in states:
        for material in state.get("materials", []):
            require(mapped_path(roots, material["path"]), material["sha256"])
        if state.get("skill_root"):
            directory = mapped_path(roots, state["skill_root"])
            for name, sha in state.get("skill_manifest", {}).items():
                require(directory / relative_path(name), sha)

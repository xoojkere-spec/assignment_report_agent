"""固定环境下运行完整回归，保留每项结果，任一失败均返回非零退出码。"""

import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UV_VERSION = "0.13.6"


def run_steps(steps, report):
    """各检查独立计时；失败后仍执行后续不依赖它的检查。"""
    records = []
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for label, command in steps:
        started = time.perf_counter()
        print(f"开始：{label}", flush=True)
        with (report / f"{label}.stdout.txt").open("wb") as out:
            with (report / f"{label}.stderr.txt").open("wb") as err:
                try:
                    code = subprocess.run(
                        command, cwd=ROOT, env=env, stdout=out, stderr=err
                    ).returncode
                except OSError as exc:
                    err.write(str(exc).encode("utf-8"))
                    code = 127
        record = {
            "name": label,
            "command": command,
            "exit_code": code,
            "seconds": round(time.perf_counter() - started, 3),
        }
        records.append(record)
        print(f"结束：{label}，退出码 {code}，耗时 {record['seconds']} 秒", flush=True)
    return records


def check_loopback():
    """先验证回环权限，避免测试在受限环境中长时间等待本地服务。"""
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(2)
        with socket.create_connection(server.getsockname(), timeout=2):
            connection, _ = server.accept()
            connection.close()


def check_test_results(junit_path, collection_output):
    """校验真实执行数量，不能把缺报告、空收集或跳过当作通过。"""
    # 参数化用例名称也可能包含这些文字，只接受单独一行的 pytest 汇总。
    match = re.search(r"^(\d+) tests? collected(?: in .+)?$", collection_output, re.MULTILINE)
    if match is None or int(match[1]) == 0:
        raise ValueError("缺少有效测试收集数量")
    root = ET.parse(junit_path).getroot()
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = root.findall("testsuite")
    else:
        raise ValueError("JUnit 根元素必须为 testsuite 或 testsuites")
    if not suites or len(list(root.iter("testsuite"))) != len(suites):
        # 当前入口消费 pytest 的平铺报告；拒绝嵌套，避免重复计算父子套件。
        raise ValueError("JUnit 必须包含平铺的 testsuite，不支持嵌套套件")

    keys = ("tests", "failures", "errors", "skipped")

    def summary_counts(element, *, required):
        values = {}
        for key in keys:
            raw = element.get(key)
            if raw is None and not required:
                continue
            if raw is None or re.fullmatch(r"[0-9]+", raw) is None:
                raise ValueError(f"JUnit 缺少或包含无效的 {key} 计数")
            values[key] = int(raw)
        return values

    def entry_counts(element, *, direct_cases=False):
        cases = element.findall("testcase") if direct_cases else list(element.iter("testcase"))
        return {
            "tests": len(cases),
            "failures": sum(1 for _ in element.iter("failure")),
            "errors": sum(1 for _ in element.iter("error")),
            "skipped": sum(1 for _ in element.iter("skipped")),
        }

    counts = dict.fromkeys(keys, 0)
    for suite in suites:
        declared = summary_counts(suite, required=True)
        actual = entry_counts(suite, direct_cases=True)
        if declared != actual:
            raise ValueError("JUnit 套件汇总与实际测试条目不一致")
        for key in keys:
            counts[key] += actual[key]

    # 核对整个报告，阻止套件外或额外嵌套的测试条目被忽略。
    if counts != entry_counts(root):
        raise ValueError("JUnit 存在未归属平铺套件的测试或结果条目")
    if root.tag == "testsuites" and any(
        value != counts[key] for key, value in summary_counts(root, required=False).items()
    ):
        raise ValueError("JUnit 根汇总与套件合计不一致")
    if counts["tests"] != int(match[1]) or any(
        counts[key] for key in ("failures", "errors", "skipped")
    ):
        raise ValueError("执行数量与收集数量不一致，或存在失败、错误、跳过")
    return counts


def finish(report, records, metadata):
    successful = bool(records) and all(record["exit_code"] == 0 for record in records)
    summary = {"passed": successful, "checks": records, **metadata}
    (report / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{'全部通过' if successful else '检查未通过'}；报告：{report}", flush=True)
    return 0 if successful else 1


def main():
    now = datetime.now(timezone(timedelta(hours=8)))
    token = uuid.uuid4().hex[:8]
    report = ROOT / "workspace" / "checks" / f"{now:%Y%m%d-%H%M%S}-{token}"
    report.mkdir(parents=True)
    expected_python = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    metadata = {
        "started_at": now.isoformat(),
        "python": platform.python_version(),
        "expected_python": expected_python,
        "expected_uv": UV_VERSION,
        "platform": platform.platform(),
        "uv_lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
    }
    records = run_steps([("uv-version", ["uv", "--version"])], report)
    uv_version = (report / "uv-version.stdout.txt").read_text(encoding="utf-8").split()
    if platform.python_version() != expected_python or uv_version[:2] != ["uv", UV_VERSION]:
        records.append({"name": "environment", "exit_code": 1})
        metadata["error"] = "Python 或 uv 与指定版本不一致，请按基线安装环境"
        return finish(report, records, metadata)

    records += run_steps(
        [("sync", ["uv", "--cache-dir", str(ROOT / ".uv-cache"), "sync", "--locked"])], report
    )
    if records[-1]["exit_code"] != 0:
        return finish(report, records, metadata)

    python = sys.executable
    collect = [
        python,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "--strict-config",
        "--strict-markers",
    ]
    records += run_steps([("collect", collect)], report)
    try:
        check_loopback()
        loopback = True
    except OSError as exc:
        loopback = False
        metadata["error"] = f"本地回环连接不可用，未启动全量测试：{exc}"
        records.append({"name": "loopback", "exit_code": 1})

    if records[-1]["exit_code"] == 0 and loopback:
        # 与报告分开，缩短 Windows Skill 快照路径；每次使用不存在的新目录。
        temp = Path(tempfile.gettempdir()) / f"aa-{token}"
        metadata["test_temp"] = str(temp)
        if temp.exists():
            records.append({"name": "temporary-directory", "exit_code": 1})
        else:
            command = [
                python,
                "-m",
                "pytest",
                "-q",
                "-ra",
                "--durations=15",
                "--strict-config",
                "--strict-markers",
                "-o",
                "xfail_strict=true",
                f"--basetemp={temp}",
                f"--junitxml={report / 'tests.junit.xml'}",
            ]
            records += run_steps([("pytest", command)], report)
            try:
                metadata["test_counts"] = check_test_results(
                    report / "tests.junit.xml",
                    (report / "collect.stdout.txt").read_text(encoding="utf-8"),
                )
            except (ValueError, OSError, ET.ParseError) as exc:
                metadata["test_result_error"] = str(exc)
                records.append({"name": "test-results", "exit_code": 1})

    records += run_steps(
        [
            ("ruff-check", [python, "-m", "ruff", "check", "."]),
            ("ruff-format", [python, "-m", "ruff", "format", "--check", "."]),
        ],
        report,
    )
    metadata["uv_lock_unchanged"] = (
        metadata["uv_lock_sha256"] == hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    )
    if not metadata["uv_lock_unchanged"]:
        records.append({"name": "lock-integrity", "exit_code": 1})
    return finish(report, records, metadata)


if __name__ == "__main__":
    raise SystemExit(main())

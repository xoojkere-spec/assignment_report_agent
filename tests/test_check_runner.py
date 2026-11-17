"""验证统一入口对失败、空收集和不完整测试结果的处理。"""

import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "check_runner", Path(__file__).resolve().parent.parent / "scripts/check.py"
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

PASSING_XML = (
    '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0">'
    '<testcase name="first" /><testcase name="second" />'
    "</testsuite></testsuites>"
)


def test_failed_command_is_preserved_and_later_check_still_runs(tmp_path):
    records = runner.run_steps(
        [
            ("bad", [sys.executable, "-c", "raise SystemExit(7)"]),
            ("later", [sys.executable, "-c", "print('still-ran')"]),
        ],
        tmp_path,
    )
    assert [record["exit_code"] for record in records] == [7, 0]
    assert "still-ran" in (tmp_path / "later.stdout.txt").read_text()
    assert runner.finish(tmp_path, records, {}) == 1
    assert not json.loads((tmp_path / "summary.json").read_text())["passed"]


@pytest.mark.parametrize("output", ["no tests collected", "0 tests collected in 0.01s"])
def test_empty_collection_cannot_pass(tmp_path, output):
    with pytest.raises(ValueError, match="收集数量"):
        runner.check_test_results(tmp_path / "not-written.xml", output)


@pytest.mark.parametrize("tests,failures,skipped", [(1, 0, 0), (2, 1, 0), (2, 0, 1)])
def test_missing_failed_or_skipped_tests_cannot_pass(tmp_path, tests, failures, skipped):
    report = tmp_path / "junit.xml"
    outcome = "<failure />" if failures else "<skipped />" if skipped else ""
    cases = f'<testcase name="first">{outcome}</testcase>'
    if tests == 2:
        cases += '<testcase name="second" />'
    report.write_text(
        f'<testsuites><testsuite tests="{tests}" failures="{failures}" '
        f'errors="0" skipped="{skipped}">{cases}</testsuite></testsuites>'
    )
    with pytest.raises(ValueError, match="执行数量"):
        runner.check_test_results(report, "2 tests collected in 0.01s")


def test_absent_report_and_empty_check_list_cannot_pass(tmp_path):
    with pytest.raises(FileNotFoundError):
        runner.check_test_results(tmp_path / "not-written.xml", "2 tests collected")
    assert runner.finish(tmp_path, [], {}) == 1


def test_complete_passing_results_can_pass(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(PASSING_XML)
    assert runner.check_test_results(report, "2 tests collected")["tests"] == 2
    assert runner.finish(tmp_path, [{"name": "test", "exit_code": 0}], {}) == 0


def test_collection_count_ignores_parametrized_node_names(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text(PASSING_XML)
    output = (
        "tests/test_check.py::test_empty[0 tests collected in 0.01s]\n"
        "tests/test_check.py::test_empty[999 tests collected]\n"
        "\n2 tests collected in 0.02s\n"
    )
    assert runner.check_test_results(report, output)["tests"] == 2


@pytest.mark.parametrize("outcome", ["failure", "error", "skipped"])
def test_junit_rejects_outcomes_hidden_by_zero_summary(tmp_path, outcome):
    root = ET.fromstring(PASSING_XML)
    ET.SubElement(root.find("testsuite/testcase"), outcome)
    report = tmp_path / "junit.xml"
    ET.ElementTree(root).write(report)
    with pytest.raises(ValueError):
        runner.check_test_results(report, "2 tests collected")


@pytest.mark.parametrize("actual_cases", [0, 1, 3])
def test_junit_rejects_case_count_different_from_summary(tmp_path, actual_cases):
    root = ET.fromstring(PASSING_XML)
    suite = root.find("testsuite")
    for case in list(suite):
        suite.remove(case)
    for index in range(actual_cases):
        ET.SubElement(suite, "testcase", name=str(index))
    report = tmp_path / "junit.xml"
    ET.ElementTree(root).write(report)
    with pytest.raises(ValueError):
        runner.check_test_results(report, "2 tests collected")


@pytest.mark.parametrize("field", ["tests", "failures", "errors", "skipped"])
def test_junit_requires_all_suite_counters(tmp_path, field):
    root = ET.fromstring(PASSING_XML)
    del root.find("testsuite").attrib[field]
    report = tmp_path / "junit.xml"
    ET.ElementTree(root).write(report)
    with pytest.raises(ValueError):
        runner.check_test_results(report, "2 tests collected")


@pytest.mark.parametrize("value", ["-1", "invalid"])
def test_junit_rejects_invalid_counter_values(tmp_path, value):
    root = ET.fromstring(PASSING_XML)
    root.find("testsuite").set("failures", value)
    report = tmp_path / "junit.xml"
    ET.ElementTree(root).write(report)
    with pytest.raises(ValueError):
        runner.check_test_results(report, "2 tests collected")


@pytest.mark.parametrize("fault", ["wrong-root", "orphan-case", "nested-suite", "root-counter"])
def test_junit_rejects_inconsistent_structure(tmp_path, fault):
    root = ET.fromstring(PASSING_XML)
    if fault == "wrong-root":
        root.tag = "unknown"
    elif fault == "orphan-case":
        ET.SubElement(root, "testcase", name="orphan")
    elif fault == "nested-suite":
        outer = ET.Element("testsuite", tests="0", failures="0", errors="0", skipped="0")
        outer.append(root.find("testsuite"))
        root = ET.Element("testsuites")
        root.append(outer)
    else:
        root.set("tests", "999")
    report = tmp_path / "junit.xml"
    ET.ElementTree(root).write(report)
    with pytest.raises(ValueError):
        runner.check_test_results(report, "2 tests collected")


def test_junit_accepts_single_suite_root(tmp_path):
    report = tmp_path / "junit.xml"
    ET.ElementTree(ET.fromstring(PASSING_XML).find("testsuite")).write(report)
    assert runner.check_test_results(report, "2 tests collected")["tests"] == 2


def test_junit_accepts_multiple_flat_suites_with_matching_root_counters(tmp_path):
    root = ET.Element("testsuites", tests="2", failures="0", errors="0", skipped="0")
    for name in ("first", "second"):
        suite = ET.SubElement(root, "testsuite", tests="1", failures="0", errors="0", skipped="0")
        ET.SubElement(suite, "testcase", name=name)
    report = tmp_path / "junit.xml"
    ET.ElementTree(root).write(report)
    assert runner.check_test_results(report, "2 tests collected")["tests"] == 2

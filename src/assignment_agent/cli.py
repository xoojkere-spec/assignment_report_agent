"""通过独立命令创建、查看、确认或恢复本地演示任务。"""

import argparse
import json
import sqlite3
import sys
from importlib.metadata import version
from pathlib import Path

from pydantic import ValidationError

from assignment_agent.model_adapter import OpenAICompatibleModel
from assignment_agent.schemas import AssignmentSpec, Clarification, Decision
from assignment_agent.service import open_agent
from assignment_agent.skills import DEFAULT_SKILL_ROOT


def main() -> None:
    # Windows 管道默认编码可能与终端不同，统一输出 UTF-8 以便解析 JSON。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="assignment-agent",
        description="作业 Agent：用户自定义 OpenAI 兼容 API 与人工确认工作流。",
    )
    parser.add_argument("--version", action="version", version=version("assignment-agent"))
    parser.add_argument("--db", default="workspace/agent.sqlite", help="SQLite 检查点文件")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="本地 API 配置文件")
    commands = parser.add_subparsers(dest="command")
    maintenance = commands.add_parser("db", help="不调用模型的数据库维护")
    database_commands = maintenance.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("status", help="只读查看数据版本")
    database_commands.add_parser("plan", help="只读查看待迁移步骤")
    backup_command = database_commands.add_parser("backup", help="停服后备份数据库和文件依赖")
    backup_command.add_argument("destination", type=Path)
    backup_command.add_argument(
        "--offline", action="store_true", required=True, help="确认已停止业务服务"
    )
    restore_command = database_commands.add_parser("restore", help="验证备份并恢复到新目录")
    restore_command.add_argument("source", type=Path)
    restore_command.add_argument("destination", type=Path)
    migrate_command = database_commands.add_parser("migrate", help="停服并备份后执行版本迁移")
    migrate_command.add_argument("--backup-directory", type=Path)
    migrate_command.add_argument(
        "--offline", action="store_true", required=True, help="确认已停止业务服务"
    )
    start = commands.add_parser("start", help="从结构化要求或作业材料创建任务")
    source = start.add_mutually_exclusive_group(required=True)
    source.add_argument("--spec", type=Path)
    source.add_argument("--assignment", type=Path)
    start.add_argument("--rubric", type=Path, action="append", default=[])
    start.add_argument("--material", type=Path, action="append", default=[])
    start.add_argument("--skill-root", type=Path, default=DEFAULT_SKILL_ROOT)
    start.add_argument("--run-id", help="可选任务 ID，默认自动生成")
    clarify = commands.add_parser("clarify", help="提交完整要求及冲突处理决定")
    clarify.add_argument("run_id")
    clarify.add_argument("--interrupt-id", required=True)
    clarify.add_argument("--spec", type=Path, required=True)
    clarify.add_argument("--note", required=True)
    for name in ("status", "resume", "approve", "revise"):
        sub = commands.add_parser(name)
        sub.add_argument("run_id")
        if name in ("approve", "revise"):
            sub.add_argument("--interrupt-id", required=True)
            sub.add_argument("--artifact-id", required=True)
            sub.add_argument("--version", required=True, type=int, dest="artifact_version")
        if name == "revise":
            sub.add_argument("--feedback", required=True)
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return
    try:
        if args.command == "db":
            from assignment_agent.migrations.backup import backup, restore
            from assignment_agent.migrations.inspection import inspect, plan
            from assignment_agent.migrations.maintenance import migrate

            if args.database_command == "backup":
                result = backup(args.db, args.destination)
            elif args.database_command == "restore":
                result = restore(args.source, args.destination)
            elif args.database_command == "migrate":
                result = migrate(args.db, args.backup_directory)
            else:
                result = inspect(args.db) if args.database_command == "status" else plan(args.db)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        from assignment_agent.migrations.runtime import access

        # 参数验证先于写入检查点，避免无效请求污染恢复状态。
        spec = (
            AssignmentSpec.model_validate_json(args.spec.read_text(encoding="utf-8-sig"))
            if args.command in ("start", "clarify") and args.spec is not None
            else None
        )
        decision = None
        if args.command == "start" and args.spec and (args.rubric or args.material):
            raise ValueError("--rubric 和 --material 需与 --assignment 一起使用")
        if args.command in ("approve", "revise"):
            decision = Decision(
                interrupt_id=args.interrupt_id,
                artifact_id=args.artifact_id,
                version=args.artifact_version,
                action="approve" if args.command == "approve" else "revise",
                feedback=getattr(args, "feedback", ""),
            )
        with access(args.db):
            pass
        with open_agent(args.db, OpenAICompatibleModel(env_file=args.env_file)) as agent:
            if args.command == "start":
                if spec is not None:
                    result = agent.start(spec, args.run_id, args.skill_root)
                else:
                    result = agent.start_materials(
                        args.assignment, args.rubric, args.material, args.run_id, args.skill_root
                    )
            elif args.command == "clarify":
                result = agent.clarify(
                    args.run_id,
                    Clarification(interrupt_id=args.interrupt_id, spec=spec, note=args.note),
                )
            elif args.command == "status":
                result = agent.status(args.run_id)
            elif args.command == "resume":
                result = agent.recover(args.run_id)
            else:
                result = agent.decide(args.run_id, decision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, ValidationError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"操作失败：{exc}\n")

# GitHub Actions 回归配置

F01-5 按用户选择先交付配置并做本地校验，远程验收等待 GitHub 仓库就绪。

远程状态：**未远程验收**。本地检查通过不能替代下述远程验收记录。

本地基线的环境、历史结果、输入指纹和故障处理见[回归基线说明](regression-baseline.md)。

## 工作流行为

- 文件：`.github/workflows/ci.yml`。
- 触发：push、pull_request、workflow_dispatch，不限定默认分支名称。
- 运行环境：GitHub 托管 `windows-2025`，uv 0.13.6，Python 版本读取 `.python-version`。
- 检查任务名：`F01 regression (Windows)`；远程设置必需状态检查时应核对实际显示名称。
- Python 安装后调用 `uv --cache-dir .uv-cache run --locked python scripts/check.py`，与本地使用同一入口。
- 任务超时为 20 分钟；不配置忽略检查失败，也不需要真实模型密钥。
- Action 固定为已核对的完整提交 SHA，旁注对应发布版本。

## 检查产物

上传步骤使用 `always()`，检查失败后仍尝试保存报告。仅选择 `workspace/checks` 中的 JSON、XML、TXT；不上传完整 workspace。

产物名包括运行 ID 和尝试次数，配置保留 14 天。若初始化阶段尚未生成报告，上传步骤给出警告，原失败步骤仍保留失败状态。任务被强制终止时，不能保证上传步骤完成。

当前先不启用依赖缓存，每次由锁文件安装；后续若需要缓存，单独验证缓存键和冷启动结果。

## 本地验证与远程验证的区别

本地使用 actionlint 1.7.12 检查 GitHub Actions 语法、字段和表达式；同时解析 YAML，检查版本约束、实际 PowerShell 命令与产物选择范围。actionlint 无法在本地证明 GitHub 的 Action 下载、触发事件和产物服务运行成功。

该校验工具下载到忽略的 `workspace/tools`，使用官方固定版本发行包并核对 SHA-256，不添加项目依赖。

2026-11-12 的本地验收：actionlint 退出码 0；两段 PowerShell 命令均通过语法解析并实际执行成功；统一检查入口确认 293 个测试全部通过，0 失败／错误／跳过，pytest 命令耗时 129.166 秒，Ruff 两项检查通过。

2026-11-13 追加两项异常路径修复后，统一入口的最新本地完整回归为 314 passed，0 失败／错误／跳过，
Ruff 与锁文件核对通过；工作流配置保持不变，最新快照见[基线说明](regression-baseline.md)。

本地 Python 已安装，未模拟 GitHub 新机器的冷启动下载；Action 下载、触发、报告上传和分支规则仍以远程验收为准。

本地可执行检查入口：

```powershell
uv --cache-dir .uv-cache run --locked python scripts/check.py
```

已有 actionlint 工具时，可执行：

```powershell
.\workspace\tools\actionlint-1.7.12\actionlint.exe -shellcheck= -pyflakes= .github/workflows/ci.yml
```

## 仓库就绪后的验收

1. 将必要源码、Skill、扫描 PDF 测试夹具、锁文件和工作流提交到 GitHub；不提交 `.env`、虚拟环境和实际任务材料。
2. 观察 push 自动运行，确认所有检查通过，并核对 JUnit 用例数与收集数量。
3. 通过 PR 验证触发行为；在隔离的测试分支放入临时失败用例，确认任务失败且仍可下载检查日志，再撤销故障注入并复测。
4. 下载报告产物，核对文件范围、失败日志及重试时的产物名称。
5. 按仓库支持的分支保护或规则集配置必需状态检查，并实际验证失败 PR 被阻止合入。

当前不宣称远程 CI、报告上传或合入阻断已验收，也不将 F01 整体标为完成。

## 官方参考

- [uv 的 GitHub Actions 集成](https://docs.astral.sh/uv/guides/integration/github/)
- [GitHub Actions 工作流语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)
- [GitHub Actions 产物说明](https://docs.github.com/en/actions/tutorials/store-and-share-data)
- [actionlint 1.7.12 发布记录](https://github.com/rhysd/actionlint/releases/tag/v1.7.12)

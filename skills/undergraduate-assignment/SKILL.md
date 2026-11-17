---
name: undergraduate-assignment
description: Help complete undergraduate course assignments by parsing the brief and rubric, confirming an outline, drafting one approved part at a time, checking evidence and DOI, and enforcing the specified word limit.
---

# 本科作业助手

用于本科课程 assignment 的要求解析、规划、逐部分写作、修改与交付检查。先读取 [入口与任务路由](references/using-research-writing/guide.md)，再只读取当前任务需要的模块。模块均位于本 skill 的 `references/` 下；不要调用名称相近的原科研写作技能。

## 核心流程

1. 新作业先读取 [作业要求解析](references/brainstorming-research/guide.md)，提取题目、rubric、课程材料、交付格式和严格字数限制。若 rubric 与文章内容冲突，或 assignment 未给字数限制，按该模块暂停询问。
2. 写作前展示大纲并取得用户确认。按已确认的大纲只写当前部分，每部分完成后展示给用户确认，再进入下一部分。多部分任务按需读取 [流程控制](references/paper-orchestration/guide.md)与[部分写作](references/writing-chapters/guide.md)。
3. 需要证据时，分别核查来源真实存在及是否支持论点；所有引用文献遵守 [文献处理](references/literature-review/guide.md)的 DOI 验证规则。写作语言遵守[写作规范](references/writing-core/guide.md)。
4. 最终按 assignment 与用户确认的统计口径核对字数、rubric 覆盖、引用和交付格式。其余专业模块按[入口与任务路由](references/using-research-writing/guide.md)按需读取。

本包内的 Python 文献工具位于 `scripts/`。这些工具仅在任务需要、依赖可用时执行；文献检索结果仍需逐条验证。本 skill 的安装路径只用于寻找模块和工具，不规定 assignment 成品的保存位置。

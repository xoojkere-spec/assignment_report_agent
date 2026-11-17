# 复用来源

`research_search.py` 的多库检索、字段归一和 DOI 去重思路，及
`paper_figures.py` 的按参考尺寸缩放裁剪逻辑，依据只读审计项目中的实现重构：

- `D:/assignment_skill/~/.codex/research-writing-skill/scripts/scholar_search.py`
- `D:/assignment_skill/tools/assemble_report.py` 的 `crop_figure`

未复制该项目的论文、图片或任务专用 Word 组装流程到正式来源库。
检索 HTTP 客户端、来源核对、SQLite 保存、恢复及人工审阅由本项目实现。
上游科研写作 Skill 的许可证如下；论文与图片的许可需分别核对，不由代码许可证授予。

MIT License

Copyright (c) 2026 旬常

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

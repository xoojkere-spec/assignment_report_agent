---
name: literature-review
description: Use when finding, organizing, and synthesizing literature for undergraduate assignments, with mandatory DOI verification for every cited literature source
allowed-tools: Read, Write, Edit, Bash, WebSearch, WebFetch
---

# 文献综述

本技能指导本科 assignment 的文献搜索、整理和综合分析。保留原有检索与整理方法，结构、资料范围和引用格式以当前作业及已确认要求为准。

<EXTREMELY-IMPORTANT>
## 核心原则：绝不编造文献

这是最重要的原则，必须严格遵守：

1. **英文文献**：可通过网络搜索获取，但必须验证真实性
2. **中文文献**：先使用实际可用的检索工具和公开来源；遇到登录、权限或全文访问障碍时，再请用户提供所需材料
3. **所有引用必须可追溯、可验证**
4. **不确定的文献信息，宁可不写也不编造**
</EXTREMELY-IMPORTANT>

<MCP-INTEGRATION>
## 文献工具脚本

本副本未内置脚本。以下示例引用本机原项目中的可选脚本，使用前检查文件、Python 环境和依赖是否可用；不能假定已安装。换机或路径失效时使用实际可用的检索、PDF 工具，不执行失效命令。

### 1. 文献搜索 (scholar_search.py)

**脚本位置**：`C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py`

**支持的数据库**：PubMed, CrossRef, Semantic Scholar, arXiv

**支持的输出格式**：
- `json` - JSON 格式（默认）
- `bibtex` - BibTeX 格式，可直接用于 LaTeX
- `ris` - RIS 格式，用于 EndNote/Zotero
- `apa` - APA 引用格式
- `mla` - MLA 引用格式
- `chicago` - Chicago 引用格式
- `vancouver` - Vancouver 引用格式

### 使用方法

```bash
# 基本搜索
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "deep learning transformer"

# 指定数据库
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "neural network" --sources pubmed,crossref

# 年份过滤（当前是2026年，建议使用近年范围）
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "machine learning" --year 2023-2026

# 输出 BibTeX 格式（用于 LaTeX 论文）
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "landslide detection" --format bibtex -o refs.bib

# 输出 APA 引用格式
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "attention mechanism" --format apa --limit 5

# JSON 输出（用于程序处理）
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "quantum computing" --format json -o results.json
```

### 输出格式示例

**BibTeX 格式**（用于 LaTeX）：
```bibtex
@article{xu2024,
  title = {CAS Landslide Dataset: A Large-Scale and Multisensor Dataset},
  author = {Yulin Xu and Chaojun Ouyang and Qingsong Xu},
  journal = {Scientific Data},
  year = {2024},
  doi = {10.1038/s41597-023-02847-z},
}
```

**APA 格式**（用于正文引用）：
```
Yulin Xu and Chaojun Ouyang (2024). CAS Landslide Dataset...
```

### 各数据库特点

| 数据库 | 速率限制 | 摘要 | 引用数 | 适用领域 |
|--------|----------|------|--------|----------|
| CrossRef | 高 | 部分 | 是 | 全学科 |
| PubMed | 中 | 需额外请求 | 否 | 生物医学 |
| Semantic Scholar | 低* | 是 | 是 | 全学科 |
| arXiv | 低 | 是 | 否 | CS/物理/数学 |

\* Semantic Scholar 建议配置 API Key 以获得更高限额。
</MCP-INTEGRATION>

## Checklist

- [ ] 确认当前部分的题目、资料需求和已确认大纲；仅在作业要求时撰写独立综述
- [ ] 生成搜索关键词（中英文）
- [ ] 英文文献：执行搜索并整理结果
- [ ] 中文文献：先尝试可用的公开检索，仅在访问受限时请求用户补充
- [ ] 按主题分类整理文献
- [ ] 生成证据-论点映射（evidence-claim map）
- [ ] 标注每条文献的引用位置（citation slot）
- [ ] 按已确认的大纲撰写当前部分，完成后等待用户确认
- [ ] 检查所有引用的真实性
- [ ] 更新已有进度或资料记录，不强制为单次检索创建新目录

## 0. 文献到正文的硬门控

文献检索与正文写作按用户请求区分。仅请求查找资料时交付核实结果，不自动写正文；为当前作业写作前，按证据驱动写作模块建立证据-论点映射，可沿用现有记录或对话：

```markdown
| Source ID | Citation | Abstract-level finding | Usable fact | Supported claim | 引用位置 / citation slot | Risk |
|---|---|---|---|---|---|---|
```

要求：

1. `Supported claim` 必须是可写进正文的一句话，不是“这篇文献很相关”。
2. `引用位置 / citation slot` 对应当前大纲中的部分及具体论点，不要求采用科研章节名称。
3. 外部事实与文献结论需有相应支撑；自己的分析需说明依据和推理。来源数量、类型和时间范围服从 assignment，不默认规定每段引用数量或要求寻找研究空白。
4. 题名与 DOI 元数据仅用于识别来源；具体结论须由实际读取的摘要、摘录或全文支持，不能由题名推断发现。

按需读取[证据驱动写作](../evidence-driven-writing/guide.md)，沿用来源存在性与论点支持关系的分别核实；本模块保留所有引用文献必须通过 DOI 验证的额外要求。

## 一、文献搜索指南

### 1.0 脚本搜索（推荐）

**使用 `C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py` 进行多数据库并行搜索。**

#### 基本用法

```bash
# 搜索所有数据库，输出 JSON
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "your query" --format json -o results.json

# 指定数据库和年份
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "deep learning" --sources crossref,semanticscholar --year 2023-2026

# 仅搜索 PubMed（生物医学）
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "hippocampus memory" --sources pubmed --limit 20
```

#### BibTeX 输出（用于 LaTeX 论文）

```bash
# 输出 BibTeX 格式
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "landslide detection" --format bibtex -o refs.bib

# 直接输出到控制台
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "transformer attention" --format bibtex --limit 5
```

**BibTeX 输出示例**：
```bibtex
@article{xu2024,
  title = {CAS Landslide Dataset: A Large-Scale and Multisensor Dataset},
  author = {Yulin Xu and Chaojun Ouyang and Qingsong Xu},
  journal = {Scientific Data},
  year = {2024},
  doi = {10.1038/s41597-023-02847-z},
  url = {https://doi.org/10.1038/s41597-023-02847-z}
}
```

#### 文本引用格式

```bash
# APA 格式
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "neural network" --format apa --limit 3

# MLA 格式
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "machine learning" --format mla --limit 3

# Chicago 格式
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "attention mechanism" --format chicago --limit 3
```

**APA 输出示例**：
```
Yulin Xu and Chaojun Ouyang (2024). CAS Landslide Dataset: A Large-Scale
and Multisensor Dataset for Deep Learning-Based Landslide Detection.
Scientific Data. 10.1038/s41597-023-02847-z
```

#### 搜索策略建议

1. **开始搜索**：使用 CrossRef（速度快、结果多、有引用数）
2. **精确搜索**：添加年份范围过滤
3. **领域搜索**：
   - 生物医学 → PubMed
   - 计算机/物理/数学 → arXiv
   - 需要 AI 推荐相关文献 → Semantic Scholar

#### JSON 输出示例

```json
[
  {
    "title": "Attention Is All You Need",
    "authors": ["Ashish Vaswani", "Noam Shazeer", "..."],
    "year": 2017,
    "journal": "Advances in neural information processing systems",
    "doi": "10.48550/arXiv.1706.03762",
    "citations": 100000,
    "url": "https://doi.org/10.48550/arXiv.1706.03762",
    "_source": "crossref"
  }
]
```

### 1.1 WebSearch 搜索（备选）

**当脚本不可用时，使用 WebSearch 进行搜索。**

**可用数据库：**

| 数据库 | 特点 | 适用领域 |
|--------|------|----------|
| Google Scholar | 综合性最强 | 全学科 |
| PubMed | 生物医学权威 | 医学、生物 |
| IEEE Xplore | 工程技术 | 计算机、电子 |
| arXiv | 预印本 | 物理、数学、CS |
| Semantic Scholar | AI增强搜索 | 全学科 |

**搜索策略：**

1. 确定核心关键词（英文）
2. 使用布尔运算符：AND, OR, NOT
3. 使用引号精确匹配："deep learning"
4. 时间范围按 assignment 和题目需要确定；不默认排除经典理论、指定阅读或较早的一手来源
5. 按与题目相关性、来源质量和证据适用性筛选；引用量只作辅助信息

### 1.2 中文文献搜索

<HARD-GATE>
先检查实际可用的工具与公开来源，不以文献语言预先判定无法检索。需要登录或权限且当前无法访问时，说明缺口并请求用户提供材料，不能声称已读取受限全文。
</HARD-GATE>

**推荐数据库：**
- **知网（CNKI）**：最全面的中文学术数据库
- **万方数据**：学位论文丰富
- **维普**：期刊论文
- **百度学术**：综合搜索

**AI提供的帮助：**

1. **生成搜索关键词**
2. **提供搜索策略建议**
3. **整理实际获取的摘要或全文；仅有摘要时明确读取范围**

## 二、文献整理

### 2.1 文献信息记录

每篇文献记录以下信息：

```markdown
## 文献1
- **标题**：
- **作者**：
- **年份**：
- **期刊/会议**：
- **DOI/链接**：
- **DOI 核实状态及元数据匹配结果**：
- **实际读取范围与定位信息**：
- **核心观点**：
- **证据类型与适用条件**：
- **主要结论**：
- **与当前作业论点的关系**：
```

### 2.2 文献分类

按主题而非按文献组织：

```
文献综述/
├── 理论基础/
├── 方法技术/
├── 应用研究/
└── 综述文章/
```

## 三、综述写作

### 3.1 综述结构

按已确认的 assignment 大纲安排材料，不自动增加独立综述或“研究空白与本研究定位”。确实要求文献综述时，可按题目需要组织为范围说明、主题综合、观点比较与评价、对题目的回应；只有任务明确需要时才讨论研究空白。

保留按主题分类、比较支持或矛盾关系、分析适用条件的做法。写作前确认大纲，每部分完成后等待用户确认，不由本模块自动连续生成整篇。

### 3.2 写作要点

**综合而非罗列**
- ❌ 张三（2020）研究了A。李四（2021）研究了B。
- ✅ 关于X问题，学界主要从三个角度展开研究：张三（2020）从A角度出发...；而李四（2021）则关注B方面...

**批判性分析**
- 不仅介绍研究内容，还要评价其贡献和局限
- 指出不同研究之间的关系（支持、补充、矛盾）

### 3.3 综述写作模板

以下是保留的可选句式示例，不是默认正文或事实来源。仅在题目确有相关任务且材料充分支持时使用，不为套模板补造研究热点、实验结果或研究空白；语言风格仍遵循已确认的核心写作规范。

**引入某一研究领域：**
```
[领域名称]是近年来[学科]研究的热点之一。自[开创性工作]以来，
该领域经历了快速发展，主要研究集中在[方向1]、[方向2]和[方向3]等方面。
```

**介绍代表性研究：**
```
[作者]（[年份]）提出了[方法/理论]，该研究[主要贡献]。
实验结果表明，[主要发现]。然而，该方法存在[局限性]。
```

**指出研究空白：**
```
尽管已有研究在[方面]取得了显著进展，但在[具体问题]方面仍存在不足。
具体而言，[问题1]尚未得到充分探讨，[问题2]缺乏系统性研究。
```

## 四、引用格式

引用样式及版本按 assignment 或用户确认要求选择，不按中文或英文自动决定。下方仅为条目结构示例，不是完整样式验证结果。格式不明确且会影响提交时先询问。

### GB/T 7714 格式示例（仅在作业指定相应版本时使用）

```
[1] 作者. 文章题目[J]. 期刊名, 年份, 卷(期): 页码.
[2] 作者. 书名[M]. 出版地: 出版社, 年份: 页码.
```

### APA 7th 格式示例（仅在作业指定时使用）

```
Author, A. A., & Author, B. B. (Year). Title. Journal, Volume(Issue), pages.
```

## 五、对话模板

### 场景0：使用脚本搜索文献（推荐）

**执行搜索命令**：

```bash
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/scholar_search.py "搜索关键词" --sources crossref,semanticscholar --year 2020-2024 --limit 20 --format json
```

**输出示例**：

> "我来使用学术搜索脚本帮你查找文献。
>
> **搜索查询**：`[关键词]`
> **数据源**：CrossRef, Semantic Scholar
> **年份范围**：2020-2024
>
> **搜索结果**（按引用数排序）：
>
> | # | 标题 | 作者 | 年份 | 期刊 | 引用数 | DOI |
> |---|------|------|------|------|--------|-----|
> | 1 | [...] | [...] | 2023 | [...] | 156 | [DOI] |
> | 2 | [...] | [...] | 2022 | [...] | 89 | [DOI] |
>
> 需要我进一步获取某篇文献的详细信息吗？"

### 场景1：用户需要英文文献（WebSearch 方式）

> "我来帮你搜索相关英文文献。
>
> **搜索关键词**：[生成的关键词]
>
> **搜索结果**：
> 1. [文献标题] - [作者] ([年份])
>    - 期刊：[期刊名]
>    - 核心内容：[简要描述]
>    - DOI：[链接]
>
> **建议**：这些文献中，[文献X]与你的研究最相关。"

### 场景2：用户需要中文文献

> "当前所需材料的访问受限，以下是可用于补充检索的策略。请提供可访问的来源或相关原文，我会继续核实。
>
> **建议搜索关键词**：
> - 主题词：[关键词1]、[关键词2]
> - 组合搜索：[关键词1] AND [关键词2]
>
> **搜索步骤**：
> 1. 访问 https://www.cnki.net/
> 2. 选择"高级搜索"
> 3. 输入上述关键词
> 4. 按本次 assignment 已确认的来源类型和时间范围筛选
>
> **请将搜索到的文献摘要粘贴给我**，我帮你整理和分析。"

### 场景3：用户提供了文献摘要

> "收到，我来帮你整理这些文献。
>
> **文献整理**：
>
> | 序号 | 作者 | 年份 | 核心观点 | 与本研究关系 |
> |------|------|------|----------|--------------|
> | 1 | [作者] | [年份] | [观点] | [关系] |
>
> **综述建议**：
> 这些文献可以从以下角度组织...
>
> 需要我帮你起草综述段落吗？"

## 六、引用检查与验证

### 6.1 引用验证（必须）

**使用 `verification` 技能进行严格验证：**

| 声称 | 验证方式 | 不充分 |
|------|----------|--------|
| 引用存在 | 验证 DOI，并核对权威注册记录或出版方元数据与所引文献匹配 | 仅能打开 DOI 或题名看起来正确 |
| 引用格式正确 | 按作业指定样式核对条目，必要时使用实际可用的格式工具 | 仅有 DOI 或成功导出文件 |
| 作者信息准确 | 搜索原始来源 | "应该没错" |

**DOI 验证要求（保留）**：

所有引用文献必须通过 DOI 验证。逐条取得实际 DOI，通过可用的 DOI 解析服务、相应注册机构记录或出版方页面核对标题、作者、年份与版本是否匹配，并保留核实依据。CrossRef 中未查到不能直接推断 DOI 无效，应检查对应注册机构或出版方记录。

DOI 缺失、无法验证或信息不匹配时，标记为未通过，暂停将该文献作为已验证依据，并询问用户是否补充 DOI 或更换可验证文献。不得伪造 DOI，也不能因为用户确认而标记通过。若指定教材、课件或其他必用材料没有 DOI，说明其与“所有文献通过 DOI 验证”的要求冲突并询问用户，不自动豁免，也不判定材料不存在。

DOI 验证只解决来源识别与对应关系，不能代替读取内容和检查论据是否支持论点。

**已有 BibTeX 文件的验证**：

读取实际待交付的 `.bib` 文件；有可用解析器时检查是否可解析、引用键是否重复及条目类型和字段是否完整，再逐条核对来源、DOI 与正文引用。没有可用解析器时可检查文本及字段，但必须说明未进行解析器验证。按作业指定引用样式检查输出格式。

搜索命令的 `--format bibtex --output` 仅生成或导出搜索结果，不验证已有文件，不能用重新搜索来覆盖或冒充参考文献验证。

### 6.2 PDF 文献解析

**用户提供 PDF 时，先确认可用解析工具。下列外部脚本存在且依赖可用时才执行；无法解析时采用其他实际可用工具，并核对关键段落与表格：**

```bash
# 提取 PDF 文本
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/pdf_parser.py paper.pdf --output paper_text.txt

# 提取结构和摘要
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/pdf_parser.py paper.pdf --sections --abstract --json paper_info.json

# 总结 PDF 内容
python C:/Users/26391/.codex/skills/undergraduate-assignment/scripts/pdf_parser.py paper.pdf --summarize
```

**输出内容**：

使用该外部 PDF 脚本时注意：JSON 的 text 字段截取前 10000 个字符，不能把它当作全文；--output 的文本输出才保存完整提取文本。自动摘要及章节识别须与原文核对，不能独立充当论据。
- 元数据（标题、作者、页数）
- 摘要
- IMRaD 章节（引言、方法、结果、讨论）
- 自动摘要

### 6.3 引用检查清单

**必须检查：**
- [ ] 所有引用的文献都真实存在（通过 DOI 验证）
- [ ] 作者、年份、标题信息准确
- [ ] 引用格式统一
- [ ] 正文引用与参考文献列表一一对应
- [ ] 每个引用支持其所在位置的具体论点，并说明与当前作业的关系

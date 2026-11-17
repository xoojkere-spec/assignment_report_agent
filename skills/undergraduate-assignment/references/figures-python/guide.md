---
name: figures-python
description: Use when creating Python data visualizations for undergraduate assignments; choose the environment, file path, figure size, resolution, colors, and export format from the assignment requirements
---

# Python 数据图表

本技能指导为本科 assignment 使用 Python 生成数据图表；环境、尺寸、分辨率、配色、输出格式和保存位置以作业或用户要求为准。

## Checklist

- [ ] 确认当前可用的 Python 环境及绘图库；仅在确实缺依赖时配置
- [ ] 确认图表类型和数据
- [ ] 记录数据清单（data manifest）
- [ ] 若使用 mock/synthetic 数据，明确标注为 planning data
- [ ] 配色清楚、可区分，并符合课程模板或用户要求
- [ ] 按实际用途和作业要求设置分辨率
- [ ] 按作业要求选择输出格式，未规定时选可正常插入交付文件的格式
- [ ] 检查中文字体显示
- [ ] 保存到用户指定位置或当前作业的合适目录

## 一、环境要求

先检查当前 Python 环境、已有绘图库及作业指定的软件版本。已有可用环境时直接使用，不强制建立名为 `research` 的 conda 环境，也不在未需要时安装依赖。

若缺少绘图所需库，选择与当前环境兼容的安装方式；安装或修改用户环境前先确认适用范围。常用库包括 matplotlib、numpy、pandas 和 seaborn，实际依赖以所用代码为准。环境配置需要专项处理时再读取 `environment-setup`，其中的固定环境名不得覆盖本作业需求。
## 二、图表规范

### 2.0 数据清单与 mock 数据边界

绘制数据图前确认所用数据及来源；按任务复杂度记录图表与数据的对应关系。用户指定路径时遵循其目录；未指定时，可以在当前作业目录中保存数据、绘图代码和输出，并在现有记录中注明位置，不强制采用 `figures/`。

| Figure | Data file or workbook | Real/mock | Source | Code or method | Outputs |
|---|---|---|---|---|---|
mock 或 synthetic 数据只允许用于规划版图表。文件名必须以 `mock_` 或 `synthetic_` 开头，并在图表、表格或章节草稿中保留 `[待真实实验替换]`。不得把 mock 数据写成“实验结果表明”。

### 2.1 分辨率与尺寸

先看 assignment、课程模板及目标文件的具体要求，再决定像素、物理尺寸、字体大小和 DPI。没有明确规定时，以实际插入后的可读性为判断依据；示例代码中的 200 DPI 只是可调整的起点，不构成提交标准。矢量输出侧重缩放后的文字与线条清晰度，不能用 DPI 数字代替实际检查。

### 2.2 输出格式

按提交文件选择 PNG、SVG、PDF 或其他课程支持的格式；只有要求多种格式时才同时导出。导出后实际打开文件，检查标签、图例、中文文字和嵌入文档后的显示效果。

### 2.3 文件尺寸

宽高与宽高比由作业页面、幻灯片或展示区域决定。没有规定时先用便于阅读的尺寸制作，再按实际交付预览调整，不套用期刊单栏或双栏宽度。
## 三、可选配色方案

### 3.1 配色示例 A

```python
NATURE_COLORS = ['#2E86AB', '#A23B72', '#F18F01', '#C73E1D', '#95C623']
```

### 3.2 配色示例 B

```python
CELL_COLORS = ['#4E79A7', '#F28E2B', '#E15759', '#76B7B2', '#59A14F', '#EDC948']
```

### 3.3 色盲友好配色

```python
COLORBLIND_SAFE = ['#0077BB', '#33BBEE', '#009988', '#EE7733', '#CC3311', '#EE3377']
```

### 3.4 配色原则

- 选择颜色时检查可区分性；默认配色可用，但要实际检查效果
- 颜色选择服从课程模板与数据含义，避免仅靠颜色传递关键信息
- 颜色数量以区分类别和保持清晰为准，不设固定上限
- 尽量使用色盲友好配色，并配合图例、线型或标记

## 四、代码模板

```python
"""
Figure X: [图表标题]
作业部分: [所属部分]
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from pathlib import Path

# 中文字体配置
CHINESE_FONT = None
font_candidates = []  # 按当前系统可用字体或课程模板填写，不固定 macOS 路径
for fp in font_candidates:
    if Path(fp).exists():
        CHINESE_FONT = fm.FontProperties(fname=fp)
        break

plt.rcParams['axes.unicode_minus'] = False

# 可选配色示例，可按课程模板调整
COLORS = ['#4E79A7', '#F28E2B', '#E15759', '#76B7B2', '#59A14F']

def setup_plot_style():
    plt.rcParams.update({
        'font.size': 10,
        'axes.titlesize': 12,
        'axes.labelsize': 10,
        'axes.spines.top': False,
        'axes.spines.right': False,
        'axes.grid': True,
        'grid.alpha': 0.3,
        'legend.frameon': False,
        'savefig.dpi': 200,  # 未另有要求时的屏幕/文档示例值
        'savefig.bbox': 'tight',
    })

def main():
    setup_plot_style()

    fig, ax = plt.subplots(figsize=(7, 5))

    # === 绑定代码 ===
    x = np.linspace(0, 10, 100)
    ax.plot(x, np.sin(x), color=COLORS[0], label='Model A')
    ax.plot(x, np.cos(x), color=COLORS[1], label='Model B')

    if CHINESE_FONT:
        ax.set_xlabel('时间 (s)', fontproperties=CHINESE_FONT)
        ax.set_ylabel('幅值', fontproperties=CHINESE_FONT)
    else:
        ax.set_xlabel('Time (s)')
        ax.set_ylabel('Amplitude')

    ax.legend()
    # === 绑定代码结束 ===

    # 保存
    output_dir = Path(__file__).parent
    fig_name = Path(__file__).stem
    plt.savefig(output_dir / f'{fig_name}.png', dpi=200)  # 按作业要求调整格式和分辨率
    # 作业要求矢量图时，再另存为 SVG/PDF；不默认同时导出。
    plt.show()

if __name__ == '__main__':
    main()
```

## 五、常用图表类型

### 折线图
```python
ax.plot(x, y, color=COLORS[0], linewidth=1.5, marker='o', markersize=4)
```

### 柱状图
```python
ax.bar(x_pos, values, color=COLORS[:len(values)], edgecolor='white')
```

### 热力图
```python
im = ax.imshow(matrix, cmap='RdBu_r', aspect='auto')
plt.colorbar(im, ax=ax)
```

### 箱线图
```python
bp = ax.boxplot(data_list, patch_artist=True)
for patch, color in zip(bp['boxes'], COLORS):
    patch.set_facecolor(color)
```

### 散点图
```python
ax.scatter(x, y, c=colors, s=sizes, alpha=0.6, cmap='viridis')
```

## 六、文件管理

### 保存位置与命名

使用用户指定目录和命名；未指定时，将代码、数据与图表放在当前作业文件附近或清楚标识的子目录中，保持引用路径可用。可采用便于理解的文件名，例如 `figure_01_comparison.py` 与相应输出图；不强制 `figures/`、`chapter1/` 或固定序号格式。
## 七、质量检查

### 图表内容
- [ ] 数据准确无误
- [ ] 坐标轴标签完整（含单位）
- [ ] 图例清晰可读

### 视觉效果
- [ ] 配色符合课程要求且类别易区分
- [ ] 分辨率符合实际交付要求，插入文档后仍清晰
- [ ] 字体大小适中

### 文件输出
- [ ] 所需输出格式已生成并能够在交付文件中正常显示
- [ ] 仅在要求多个格式时检查每一种输出
- [ ] 文件命名规范

## 八、常见问题

### Q1：中文显示为方块

先查找当前系统实际可用的中文字体，并确认字体文件路径，再设置 FontProperties；下方路径是占位说明，不能直接运行。

```python
from matplotlib.font_manager import FontProperties
font = FontProperties(fname='实际已确认存在的字体文件路径')
ax.set_xlabel('中文标签', fontproperties=font)
```

### Q2：图片模糊

```python
plt.savefig('figure.png', dpi=200, bbox_inches='tight')  # 按交付要求调整
```

### Q3：图例遮挡数据

```python
ax.legend(loc='upper left', bbox_to_anchor=(1.02, 1))
```

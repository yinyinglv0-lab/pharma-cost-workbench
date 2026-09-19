# -*- coding: utf-8 -*-
"""报告模板解析（子任务2 前置）：识别 {{占位符}} 并三分类。

- 数值类：计算层直算（数据填充替换），绝不经过 LLM；
- 表格类：计算层生成多行数据；
- 文本类：子任务3/4（RAG检索 + LLM生成）填充；
- 引用类：知识库来源清单（子任务3 检索结果）。
"""
import re
from pathlib import Path

from docx import Document
from paths import DATA_DIR

# Deployed templates live with configured report data, separately from code.
TEMPLATE_PATH = DATA_DIR / "月度成本分析报告模板.docx"
PH = re.compile(r"\{\{(.*?)\}\}")

# 表格类关键词（占位符名含"表格"即表格类）
_TABLE_KW = ["表格", "清单", "跟踪"]
# 文本类关键词
_TEXT_KW = ["分析文本", "排查分析", "拆解", "亮点", "问题", "说明", "标题", "类型", "日期",
            "编号", "告警描述", "趋势", "引用"]
# 其余默认数值类


def _classify(name: str) -> str:
    if any(k in name for k in _TABLE_KW):
        return "表格"
    if any(k in name for k in _TEXT_KW):
        return "文本"
    return "数值"


def parse_template(template_path=None) -> dict:
    """解析模板 → 占位符注册表 {name: kind}（覆盖段落与表格单元格）。"""
    path = Path(template_path) if template_path else TEMPLATE_PATH
    doc = Document(str(path))
    found = {}
    def inspect(container):
        for par in container.paragraphs:
            for match in PH.finditer(par.text):
                found.setdefault(match.group(1), _classify(match.group(1)))
        for table in container.tables:
            for row in table.rows:
                for cell in row.cells:
                    inspect(cell)
    inspect(doc)
    for section in doc.sections:
        for part in (section.header, section.footer, section.first_page_header,
                     section.first_page_footer, section.even_page_header, section.even_page_footer):
            inspect(part)
    return found


def registry(template_path=None) -> dict:
    return parse_template(template_path)
#（注：内容由AI生成）

"""Small presentation layer. All financial values are computed by domain services."""
from html import escape

import streamlit as st

# Keep sidebar geometry native so collapsing it releases the content width.
# Native widgets retain focus, accessibility labels and validation behavior.
STYLE = """
<style>
.stMainBlockContainer {max-width:1480px;padding-top:4rem;padding-bottom:3rem;}
@media(min-width:900px) and (max-width:1500px){.stMainBlockContainer{padding-left:2rem;padding-right:2rem;}}
@media(max-width:640px){.stMainBlockContainer{padding-left:1rem;padding-right:1rem;padding-top:3.5rem;}}
.wb-banner{border-bottom:1px solid #dce5e7;padding:8px 0 20px;margin-bottom:8px;}
.wb-banner .eyebrow{color:#0f766e;font-size:12px;font-weight:600;letter-spacing:0;margin-bottom:8px;}
.wb-banner h2{margin:0 0 8px;font-size:24px;line-height:1.4;color:#202a35;}
.wb-banner p{color:#4b5563;font-size:14px;line-height:1.7;margin:0;}
.wb-kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin:4px 0 12px;}
.wb-kpi{background:#fff;border:1px solid #dfe6ef;border-radius:8px;padding:18px 18px 16px;min-width:0;box-shadow:0 2px 6px rgba(30,58,90,.025);}
.wb-kpi .name{color:#475569;font-size:13px;line-height:1.5;margin-bottom:10px;}
.wb-kpi .value{color:#172b4d;font-size:24px;font-weight:650;line-height:1.25;font-variant-numeric:tabular-nums;overflow-wrap:anywhere;}
.wb-kpi .note{color:#64748b;font-size:12px;line-height:1.5;margin-top:10px;}
.wb-kpi .rise{color:#b73b36;}.wb-kpi .fall{color:#2166a5;}
.wb-process{display:flex;flex-wrap:wrap;gap:8px 20px;list-style:none;padding:8px 0 14px;margin:2px 0 8px;border-bottom:1px solid #dfe6ef;}
.wb-process li{display:flex;align-items:center;gap:8px;font-size:13px;color:#64748b;}
.wb-process .number{display:inline-grid;place-items:center;width:24px;height:24px;border-radius:50%;background:#e7edf5;color:#475569;font-size:12px;font-weight:600;}
.wb-process .active{color:#1e3a8a;font-weight:600;}.wb-process .active .number{background:#1e3a8a;color:white;}
@media(max-width:1100px){.wb-kpis{grid-template-columns:repeat(2,minmax(0,1fr));}}
@media(max-width:640px){.wb-kpis{gap:10px;}.wb-kpi{padding:14px 12px;}.wb-banner{padding:8px 0 20px;}}
</style>
"""


def apply_design():
    st.html(STYLE)


def page_header(title, description):
    st.title(title)
    st.caption(description)


def banner(title, description, eyebrow='经营分析 · 成本管理'):
    st.html(f'<section class="wb-banner"><div class="eyebrow">{escape(eyebrow)}</div>'
            f'<h2>{escape(title)}</h2><p>{escape(description)}</p></section>')


def kpis(items):
    """Each tuple is (label, formatted value, note, optional rise/fall)."""
    cards = []
    for item in items:
        label, value, note = item[:3]
        color = item[3] if len(item) > 3 and item[3] in ('rise', 'fall') else ''
        cards.append(f'<div class="wb-kpi" role="listitem"><div class="name">{escape(str(label))}</div>'
                     f'<div class="value {color}">{escape(str(value))}</div><div class="note">{escape(str(note))}</div></div>')
    st.html('<div class="wb-kpis" role="list" aria-label="关键指标">' + ''.join(cards) + '</div>')


def process_steps(labels, active=None):
    nodes = ''.join(f'<li class="{"active" if index == active else ""}"'
                    + (' aria-current="step"' if index == active else '')
                    + f'><span class="number">{index + 1}</span>{escape(label)}</li>'
                    for index, label in enumerate(labels))
    st.html('<ol class="wb-process" aria-label="操作流程">' + nodes + '</ol>')


def money_columns(*names):
    return {name: st.column_config.NumberColumn(format='accounting', alignment='right') for name in names}

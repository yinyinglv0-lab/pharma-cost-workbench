# -*- coding: utf-8 -*-
"""本地 ECharts HTML 渲染；响应式布局和内容高度交由 st.iframe 测量。"""
import hashlib
import json
import tempfile
from pathlib import Path

import streamlit as st

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_CACHE_DIR = Path(tempfile.gettempdir()) / "project4_chart_cache"
TEMPLATE_VERSION = 4
_ECHARTS_JS = None


def _load_echarts_js():
    global _ECHARTS_JS
    if _ECHARTS_JS is None:
        _ECHARTS_JS = (_ASSETS / "echarts.min.js").read_text(encoding="utf-8")
    return _ECHARTS_JS


def _safe_json(value):
    # Inline script contents must not be terminated by a data label.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).replace("<", "\\u003c")


def _multi_chart_html(charts):
    divs, inits = [], []
    for i, item in enumerate(charts):
        opt, height = item[:2]
        full = len(item) > 2 and item[2]
        divs.append(f'<div id="c{i}" class="chart{" full" if full else ""}" style="height:{int(height)}px"></div>')
        inits.append(f"""
        var chart = echarts.init(document.getElementById('c{i}'));
        chart.setOption({_safe_json(opt)});
        charts.push(chart);
        """)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>
html, body {{ margin: 0; background: #ffffff; }}
.grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; padding: 4px; }}
.chart {{ width: 100%; min-width: 0; min-height: 400px; border: 1px solid #dfe6ef; border-radius: 8px; box-sizing: border-box; }}
.full {{ grid-column: 1 / -1; }}
@media (max-width: 700px) {{ .grid {{ grid-template-columns: minmax(0, 1fr); }} }}
</style></head><body><div class="grid">{''.join(divs)}</div><script>
{_load_echarts_js()}
(function() {{
    if (!window.echarts) return;
    var charts = [];
    {''.join(inits)}
    var frame;
    function resizeCharts() {{
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(function() {{
            charts.forEach(function(chart) {{
                var el = chart.getDom();
                if (chart.isDisposed() || !el.clientWidth || !el.clientHeight) return;
                if (chart.getWidth() !== el.clientWidth || chart.getHeight() !== el.clientHeight) {{
                    chart.resize();
                }}
            }});
        }});
    }}
    resizeCharts();
    window.addEventListener('resize', resizeCharts);
    var observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resizeCharts) : null;
    if (observer) charts.forEach(function(chart) {{ observer.observe(chart.getDom()); }});
    window.addEventListener('pagehide', function() {{
        window.removeEventListener('resize', resizeCharts);
        cancelAnimationFrame(frame);
        if (observer) observer.disconnect();
        charts.forEach(function(chart) {{ chart.dispose(); }});
    }});
}})();
</script></body></html>"""


def _chart_html(option_json: str, height: int) -> str:
    """兼容单图调用，使用整行宽度。"""
    return _multi_chart_html([(json.loads(option_json), height, True)])


def render_echarts_multi(charts):
    """charts: [(option, height, full_width), ...]，窄屏自动单列。"""
    charts = list(charts)
    if not charts:
        return
    # 高度和跨列状态也影响 HTML，必须参与缓存键。
    payload = _safe_json(charts)
    key = hashlib.sha256(f"{payload}|{TEMPLATE_VERSION}".encode("utf-8")).hexdigest()[:20]
    path = _CACHE_DIR / f"chart_{key}.html"
    if not path.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(_multi_chart_html(charts), encoding="utf-8")
    st.iframe(path, height="content")
#（注：内容由AI生成）

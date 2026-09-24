"""Actual ECharts -> Streamlit event bridge with server-validated point context."""
from __future__ import annotations

import hashlib
import json
from .echarts_helper import _load_echarts_js

_CSS='''
.linked-grid {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;padding:4px;}
.linked-chart {width:100%;min-width:0;min-height:400px;border:1px solid #dfe6ef;border-radius:8px;box-sizing:border-box;background:#fff;}
.linked-chart.full {grid-column:1/-1;}
.linked-help{color:var(--st-secondary-color,#64748b);font:12px/1.6 var(--st-font,sans-serif);margin:0 4px 6px;}
@media(max-width:700px){.linked-grid{grid-template-columns:minmax(0,1fr);}}
'''
_JS=r'''
export default function(component) {
  const {parentElement,data,setTriggerValue}=component;
  const grid=parentElement.querySelector('.linked-grid');
  const instances=[];grid.replaceChildren();
  data.charts.forEach((item,index)=>{
    const el=document.createElement('div');
    el.className='linked-chart'+(item.full?' full':'');el.style.height=item.height+'px';
    el.dataset.chartIndex=String(index);grid.appendChild(el);
    const chart=globalThis.echarts.init(el);
    chart.setOption(item.option);instances.push(chart);
    chart.on('click',params=>{
      if(params.componentType!=='series' || !Number.isInteger(params.dataIndex) || !Number.isInteger(params.seriesIndex))return;
      setTriggerValue('selected',{context_id:data.context_id,chart_index:index,
        series_index:params.seriesIndex,data_index:params.dataIndex,event_id:crypto.randomUUID()});
    });
  });
  let frame;
  const resize=()=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>instances.forEach(c=>{const el=c.getDom();if(!c.isDisposed()&&el.clientWidth&&el.clientHeight)c.resize();}));};
  const observer=new ResizeObserver(resize);instances.forEach(c=>observer.observe(c.getDom()));
  window.addEventListener('resize',resize);resize();
  return ()=>{observer.disconnect();window.removeEventListener('resize',resize);cancelAnimationFrame(frame);instances.forEach(c=>c.dispose());};
}
'''


def chart_context_id(product,specification,month,scope_token):
    return hashlib.sha256(json.dumps([product,specification,month,scope_token],ensure_ascii=False).encode()).hexdigest()


def decode_chart_selection(event,charts,*,context_id,month,available_months):
    """Derive identity from trusted options, never client-supplied labels/values."""
    if not isinstance(event,dict) or event.get('context_id')!=context_id:
        return None
    indexes=[event.get(key) for key in ('chart_index','series_index','data_index')]
    if any(type(value) is not int or value<0 for value in indexes):
        return None
    ci,si,di=indexes
    try:
        option=charts[ci][0];series=option['series'][si];point=series['data'][di]
        value=point.get('value') if isinstance(point,dict) else point
        if value is None or value=='-' or series.get('silent') or (isinstance(point,dict) and point.get('stack_gap')):
            return None
        selected_month=month;label=series.get('name','单位成本');comparison=None
        axes=option.get('xAxis',{})
        axis=axes[0] if isinstance(axes,list) else axes
        if series['type']=='line':
            selected_month=axis['data'][di]
        elif series['type']=='pie':
            label=point.get('name','单位成本')
        elif series['type']=='heatmap':
            if len(value)<5 or value[2] is None:return None
            selected_month,label=value[3:5]
            comparison='同比' if len(value)>5 and value[5] in ('yoy','同比') else '环比'
        elif series['type']=='bar':
            if series.get('name')=='基底':return None
            label=axis['data'][di]
            if label=='上月':
                import pandas as pd
                selected_month=str(pd.Period(month,freq='M')-1)
        else:
            return None
    except (KeyError,IndexError,TypeError,ValueError):
        return None
    if selected_month not in available_months:
        return None
    label=str(label)
    element=next((element for element,terms in (
        ('材料',('材料',)),('人工',('人工',)),('制费',('制费','制造费用')))
        if any(term in label for term in terms)), '单位成本')
    selection={'month':selected_month,'element':element,'event_id':str(event.get('event_id',''))[:80]}
    if comparison:selection['comparison']=comparison
    return selection


def safe_inline_option(option):
    """Use canvas tooltip text: business names must never become HTML handlers."""
    from copy import deepcopy
    import re
    result=deepcopy(option)
    def visit(node):
        if isinstance(node,dict):
            for name,value in node.items():
                if name=='tooltip' and isinstance(value,dict):
                    value['renderMode']='richText'
                    if isinstance(value.get('formatter'),str):
                        value['formatter']=re.sub(r'<br\s*/?>','\n',value['formatter'],flags=re.I)
                visit(value)
        elif isinstance(node,list):
            for item in node:visit(item)
    result.setdefault('tooltip',{})['renderMode']='richText'
    visit(result)
    return result


def render_linked_echarts(charts,*,context_id,month,available_months,key):
    from app_pages.component_registry import inline_component
    charts=list(charts)
    renderer=inline_component('linked_cost_charts',
        html='<div class="linked-grid"></div>',css=_CSS,js=_load_echarts_js()+'\n'+_JS)
    result=renderer(key=key,data={'charts':[{'option':safe_inline_option(c[0]),'height':int(c[1]),'full':bool(c[2]) if len(c)>2 else False} for c in charts],
                                    'context_id':context_id},height='content',on_selected_change=lambda:None)
    return decode_chart_selection(result.selected,charts,context_id=context_id,month=month,available_months=available_months)


def rag_question(product,specification,month,element='单位成本',*,comparison='环比'):
    focus={'材料':'原材料计价、领退料与工艺收率','人工':'工序工时、定员与工资归集',
           '制费':'设备折旧、能源消耗与费用分配','单位成本':'原材料、人工、工艺与制造费用'}[element]
    base='上年同月' if comparison=='同比' else '上月'
    cost_label=element if element=='单位成本' else element+'成本'
    return f'{product}（{specification}）{month}{cost_label}{comparison}变动（与{base}相比）与{focus}有哪些原文依据？'

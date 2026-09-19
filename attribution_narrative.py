"""Conclusion-first narrative rendered only from calculated JSON facts."""

def render(payload, explanations=None):
    from attribution_gen import LABELS, _num, _short_field, SHORT_ACTIONS
    facts=payload['facts']
    if not facts.get('available'):
        note=f"{payload['product']} {payload['month']}：{facts.get('reason')}。不生成跨月归因。"
        return note, [], note
    elements=payload['elements']
    lead=max(elements,key=lambda k:abs(elements[k]['change_amount']))
    total=payload['金额口径']['总变动额']
    output=sum(e['output_effect'] for e in elements.values())
    unit=sum(e['unit_cost_effect'] for e in elements.values())
    overview=f"{payload['month']}总成本变动{_num(total,True)}元，其中产量影响{_num(output,True)}元、单位成本影响{_num(unit,True)}元。"
    if total:
        overview+=f"产量和单位成本影响分别占净变动的{_num(output/total*100)}%和{_num(unit/total*100)}%。"
    overview+=f"{LABELS[lead]}是金额变动绝对值最大的要素。"
    if output<0:overview+='产量减少导致的支出下降不等于成本管控节约；单位成本下降也需核实业务原因。'
    sections=[]
    for key in sorted(elements,key=lambda k:abs(elements[k]['change_amount']),reverse=True):
        e=elements[key]; f=facts['elements'][key]
        ratio=f"{_num(f['mom_pct'],True)}%" if f['mom_pct'] is not None else '不可计算（上月为零）'
        contrib=f"金额贡献度{_num(e['contribution_pct'])}%" if e['contribution_pct'] is not None else '净变动为零，贡献度无定义'
        direction='上涨' if f['unit_delta']>0 else '下降' if f['unit_delta']<0 else '持平'
        first=f"{LABELS[key]}单位成本{direction}{_num(abs(f['unit_delta']))}元/盒（环比{ratio}），{contrib}。"
        refs=list(f.get('evidence_ids',[])) + [x['id'] for x in e.get('evidence',[])]
        if e['analysis_level']=='brief':
            text=first+'本月单位成本变动较小，保持常规监测。'
        else:
            driver={'output':'产量变化','unit_cost':'单位成本变化','balanced':'产量与单位成本共同变化','none':'无净影响'}[e['dominant_driver']]
            text=first+f"金额变动{_num(e['change_amount'],True)}元，以{driver}影响为主（产量影响{_num(e['output_effect'],True)}元，单位成本影响{_num(e['unit_cost_effect'],True)}元）。"
            top=e.get('top_materials',[])
            if key=='材料' and e['price_effect'] is not None:
                pd=e['price_usage_driver']; label={'price':'价格因素为主','usage':'折算单耗因素为主','balanced':'两项影响相当','none':'两项均无影响'}.get(pd['dominant'],'')
                text+=f"\n市场参考量价测算：价格因素{_num(e['price_effect'],True)}元，折算单耗因素{_num(e['usage_effect'],True)}元，{label}"
                if pd['relationship']=='offset':text+='，两项方向相反、相互抵消'
                text+='。'
                for unmatched in e.get('unmatched_materials',[]):
                    if unmatched.get('unit_cost_effect') is not None:
                        text+=f"\n{unmatched['name']}未匹配市场参考价，单位成本影响{_num(unmatched['unit_cost_effect'],True)}元，单独保留而不归入药材量价因素。"
                if abs(e.get('unallocated_residual',0))>.01:
                    text+=f"尚未勾稽分配部分{_num(e['unallocated_residual'],True)}元，需补齐原始明细核对。"
                for m in top[:2]:
                    text+=f"\n{m['name']}：参考价{_num(m['reference_price_before'])}→{_num(m['reference_price_after'])}元/kg，折算单耗{m['reference_usage_before']:.5f}→{m['reference_usage_after']:.5f}kg/盒；价格影响{_num(m['price_effect'],True)}元、折算单耗影响{_num(m['usage_effect'],True)}元。"
                text+='以上为市场参考假设下的分解，不代表实际采购价格或实物耗用变化。'
                rising=e.get('reference_usage_increase_names',[])
                if len(rising)>1:
                    text+='、'.join(rising)+'的参考折算单耗同时上升，需核对采购计价、批次结构及收率，不能据此认定实物耗用或生产效率恶化。'
            elif key=='材料':
                text+='未获得可匹配的市场参考价，本月不生成价格和单耗测算。'
            row=explanations['elements'][key] if explanations else {}
            if key=='材料':
                names='、'.join(m['name'] for m in top[:2]) or '主要变动原材料'
                action=f"建议：采购部逐项比对{names}本月结算价与合同执行价，检查是否有调价约定；生产部对比{payload['month']}与{facts['previous_month']}相关批次的投料、产出及收率记录，定位差异批次。"
            elif key=='人工':
                action=f"建议：生产部对比{payload['month']}与{facts['previous_month']}工时和产量，财务部核查加班工资及跨期计提，区分用工投入与每工时人工成本变化。"
            else:
                details=f.get('detail',[])
                name=details[0]['name'] if details else '主要费用项目'
                action=f"建议：财务部对比{payload['month']}与{facts['previous_month']}的{name}凭证和分配基数，逐项区分支出变化、归集期间与产量摊薄影响。"
                if facts['current']['volume'] < facts['previous']['volume'] and f['unit_delta']<0:
                    text+='产量与单位制造费用同时下降，需核查费用支出、固定与变动费用分配及跨期归集，不能仅解释为产量摊薄。'
            if row:
                hypothesis=_short_field(row.get('hypothesis'),'','hypothesis',80)
                if hypothesis:text+='\n业务解释：'+hypothesis
                # Preserve the data-specific action even if the model supplies a generic recommendation.
                extra=_short_field(row.get('recommendation'),'','recommendation',100)
                if extra and any(m['name'] in extra for m in top[:2]):action=extra
                refs+=row.get('evidence_ids',[])
            text+='\n'+action
        sections.append({'element':key,'title':LABELS[key], 'text':text,'evidence_ids':list(dict.fromkeys(refs)), 'analysis_level':e['analysis_level']})
    parts=[overview]+[s['text'] for s in sections]
    if payload['告警_环比超正负10%']:
        parts.append('重点告警：'+'；'.join(f"{a['要素']}环比{_num(a['环比%'],True)}%" for a in payload['告警_环比超正负10%'])+'，严格超过±10%，优先按上述建议核查。')
    return overview,sections,'\n\n'.join(parts)

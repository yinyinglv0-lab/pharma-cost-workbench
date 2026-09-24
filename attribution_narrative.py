"""Source excerpt gates and backwards-compatible attribution renderer.

The shared deterministic contract lives in enterprise.analysis_narrative. These
quote helpers remain public because existing model validators also use them.
"""
from pathlib import PureWindowsPath
import re

from enterprise.numeric import format_number as number, format_percent


_QUOTE_WORDS = {
    '材料': ('收率', '投料', '损耗', '原料', '材料', '耗用', '填充', '配方', 'RSD', 'rsd', '均匀性'),
    '人工': ('工时', '定员', '人工', '工资', '返工'),
    '制费': ('折旧', '能源', '能耗', '蒸汽', '设备', '维修', '制造费用'),
}
_QUOTE_INSTRUCTION = re.compile(
    r'忽略.{0,20}(?:指令|提示|规则|约束|要求)|(?:系统|开发者|角色)提示|你是|你必须|'
    r'(?:输出|返回|生成|复述|重复).{0,20}(?:JSON|答案|数字|数值|百分比|hypothesis|recommendation)|'
    r'(?:system|assistant|developer)\s*:|ignore.{0,30}instructions|'
    r'hypothesis|recommendation|evidence_ids|https?://|<[^>]+>|```', re.I)
_QUOTE_HEADING = re.compile(r'(?:影响|关联|分配|分析|概述|要求|规范|指标|流程|路线|说明|需求)(?:表)?[。:：]?\s*$')
_QUOTE_STANDARD = re.compile(
    r'(?:收率|合格率|损耗|装量|投料量|耗量|工时|定员|温度|压力|能耗|折旧率|RSD|相对标准偏差)\s*'
    r'(?:应|须|需|为|是|约|达到|不得低于|不得超过|不低于|不高于)?\s*'
    r'(?:[≥≤><=]|不超过|至少|最多|不低于|不高于)?\s*\d+(?:\.\d+)?\s*'
    r'(?:[%％℃]|kg|千克|公斤|人|小时|h|kWh|MPa)', re.I)
_QUOTE_MECHANISM = re.compile(
    r'.{2,}(?:导致|造成|增加|减少|降低|提高|浪费|报废|计入|归集至|计提|影响)'
    r'.*(?:材料|原料|耗用|损耗|成本|人工|工时|工资|能耗|能源|蒸汽|费用)|'
    r'(?:材料|原料|耗用|损耗|成本|人工|工时|工资|能耗|能源|蒸汽|费用)'
    r'.{0,16}(?:增加|减少|降低|提高|浪费|报废)|'
    r'(?:应|须|需|不得|要求).{0,40}(?:核对|核查|复核|控制|记录|计量|分配|检验)|'
    r'(?:工时|工资|费用|折旧).{0,16}按.{1,20}(?:分配|计提|归集|记录)|'
    r'配方.{0,40}(?:占比|比例|占).{0,12}\d+(?:\.\d+)?\s*[%％]')


def is_substantive_quote(text, element):
    """Conservative excerpt gate, not semantic proof or a current-event claim."""
    from enterprise.knowledge_applicability import matching_view
    if not isinstance(text, str) or not 8 <= len(text) <= 260:
        return False
    view = matching_view(text)
    if _QUOTE_INSTRUCTION.search(view):
        return False
    words = _QUOTE_WORDS.get(element)
    if not words:
        return False
    # Do not borrow an element word from a heading to license an unrelated row.
    for line in re.split(r'[。；！？\n]+', view):
        line = line.strip()
        staffing = element == '人工' and re.search(r'\d+\s*人\s*[/每]\s*班', line)
        if not staffing and not any(word in line for word in words):
            continue
        if _QUOTE_HEADING.search(line) and not re.search(r'[，,]|应|须|需.{1,}|导致|造成', line):
            continue
        if staffing or _QUOTE_STANDARD.search(line) or _QUOTE_MECHANISM.search(line):
            return True
    return False


def cited_quote(sources, evidence_ids, element):
    """One substantive, contiguous original excerpt from an actually cited source."""
    from enterprise.knowledge_applicability import matching_view
    words = _QUOTE_WORDS.get(element)
    if not words:
        return None
    for source in sources or []:
        if (not isinstance(source, dict) or source.get('id') not in evidence_ids
                or source.get('kind') != 'document_basis'
                or element not in source.get('elements', [])
                or source.get('evidence_role', 'document_basis') != 'document_basis'
                or source.get('support_status', 'eligible') != 'eligible'):
            continue
        raw = source.get('text', '')
        if not isinstance(raw, str) or _QUOTE_INSTRUCTION.search(matching_view(raw)):
            continue
        pieces = list(re.finditer(r'[^。；！？\n]+[。；！？]?', raw))
        quote = None
        for index, piece in enumerate(pieces):
            if not any(word in matching_view(piece.group()) for word in words):
                continue
            # A table header can only accompany a real staffing row.
            for end in range(index, min(index+4, len(pieces))):
                candidate = raw[piece.start():pieces[end].end()].strip()
                if len(candidate) > 260:
                    break
                if is_substantive_quote(candidate, element):
                    last = pieces[end].group().strip()
                    start = pieces[end].start() if is_substantive_quote(last, element) and any(
                        word in matching_view(last) for word in words) else piece.start()
                    stop = pieces[end].end()
                    if end + 1 < len(pieces) and re.match(r'\s*[↑↓]\s*\d', pieces[end+1].group()):
                        continued = raw[start:pieces[end+1].end()].strip()
                        if is_substantive_quote(continued, element):
                            stop = pieces[end+1].end()
                    quote = raw[start:stop].strip()
                    break
            if quote:
                break
        if quote is None:
            continue
        meta = source.get('source') or {}
        filename = PureWindowsPath(str(meta.get('file') or meta.get('document_id') or '工艺资料')).name
        return {'id': source['id'], 'quote': quote, 'source': meta,
                'text': f'《{filename}》写明“{quote}”[{source["id"]}]，可据此核对本期记录。'}
    return None


def narrative_references(sources, refs):
    """Retain all already-admitted IDs; callers own authorization/validation."""
    return list(dict.fromkeys(refs))


def _marks(refs):
    return ' '.join(f'[{ref}]' for ref in dict.fromkeys(refs) if ref)


def _labor_text(facts, refs):
    """Legacy private helper retained; the shared renderer handles generic units."""
    from enterprise.analysis_narrative import _labor_observation, _settings
    lines, identifiers = _labor_observation(facts.get('labor_factors', {}), _settings(None, '元', '盒'))
    refs.extend(identifiers)
    return '\n'.join(lines) or '现有人工记录不足以区分工时与小时归集费用，未推定具体业务原因。'


def render(payload, explanations=None, sources=None, *, detailed=False,
           amount_unit='元', reporting_unit='盒', industry_comparison=None,
           observed_yields=None, config=None):
    """Preserve the legacy (overview, sections, text) tuple and keyword contract."""
    from enterprise.analysis_narrative import build_attribution_narrative
    result = build_attribution_narrative(
        payload, explanations, sources, detailed=detailed,
        amount_unit=amount_unit, reporting_unit=reporting_unit,
        industry_comparison=industry_comparison, observed_yields=observed_yields, config=config)
    return result['overview'], result['sections'], result['text']

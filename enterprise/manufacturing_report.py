"""Read-only DOCX/PDF reading copies of an authenticated frozen manufacturing run.

The service must call get_run first for current authorization. This module validates
only the supplied snapshot and its pinned profile/scope: it never loads current
facts, models, retrieval, tasks, templates, caller-supplied paths or live profiles.
JSON remains the audit authority. Exports are unsigned reading copies, not approval
records, and do not retrofit the legacy pharmaceutical report renderer.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
from threading import RLock
from xml.sax.saxutils import escape

SCHEMA_VERSION = 'manufacturing-analysis/1'
EXPORT_VERSION = 'manufacturing-reading-copy/1'
BLUE = '2A78D6'
BLACK = '0B0B0B'
_ELEMENTS = ('material', 'labor', 'overhead')
_MODES = ('attribution', 'benchmark')
_REF = re.compile(r'\[([A-Za-z][A-Za-z0-9_-]{0,127})\]')
_LOCK = RLock()


class ManufacturingReportError(ValueError):
    """Unsupported, corrupt or cross-scope frozen reading input."""


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _fail(message):
    raise ManufacturingReportError(message)


def _month(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])', value) or value[:4] == '0000':
        _fail('冻结期间格式无效')
    return int(value[:4]) * 12 + int(value[5:])


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _row_scope(row, scope, *, factory=None, month=None, required=False):
    if row is None and not required:
        return
    if not isinstance(row, dict):
        _fail('冻结事实缺少明确范围记录')
    for field, expected in (('product', scope['product']), ('specification', scope['specification']),
                            ('factory', factory), ('month', month)):
        if expected is not None and row.get(field) != expected:
            _fail('冻结事实范围不一致：' + field)
    if row.get('factory') not in (scope['home_factory'], scope['peer_factory']):
        _fail('冻结事实包含其他工厂')
    if row.get('month') not in (scope['month'], scope.get('previous_month')):
        _fail('冻结事实包含其他期间')


def _source_scope(source, scope):
    declared = source.get('scope') or {}
    if not isinstance(declared, dict):
        _fail('来源范围结构无效')
    for singular, plural in (('product', 'products'), ('specification', 'specifications')):
        raw = declared.get(plural, declared.get(singular))
        if raw is not None:
            values = raw if isinstance(raw, list) else [raw]
            if scope[singular] not in values and '*' not in values:
                _fail('来源与所选产品规格不一致')
    if source.get('kind') in ('accounting_fact', 'data_fact', 'observed_fact'):
        for record in _leaves(source.get('source') or {}):
            key = record.get('key') or {}
            for field, alias in (('product', '产品名称'), ('specification', '产品规格')):
                actual = key.get(field, key.get(alias))
                if actual is not None and actual != scope[field]:
                    _fail('核算来源包含其他产品规格')
            actual = key.get('factory', key.get('工厂'))
            if actual is not None and actual not in (scope['home_factory'], scope['peer_factory']):
                _fail('核算来源包含其他工厂')
            months = key.get('month', key.get('月份'))
            if months is not None:
                months = months if isinstance(months, list) else [months]
                if any(value not in (scope['month'], scope.get('previous_month')) for value in months):
                    _fail('核算来源包含其他期间')


def validate_manufacturing_run(frozen_run):
    """Validate integrity and pinned semantics, without granting authorization."""
    if not isinstance(frozen_run, dict) or frozen_run.get('schema_version') != SCHEMA_VERSION:
        _fail('制造业冻结分析结构版本不受支持')
    try:
        snapshot = deepcopy(frozen_run)
        core = {key: value for key, value in snapshot.items() if key not in ('analysis_run_id', 'analysis_hash')}
        if not re.fullmatch(r'ma_[a-f0-9]{32}', str(snapshot.get('analysis_run_id', ''))):
            _fail('冻结分析标识无效')
        if not re.fullmatch(r'[a-f0-9]{64}', str(snapshot.get('analysis_hash', ''))) or _digest(core) != snapshot['analysis_hash']:
            _fail('冻结分析完整性校验失败')
        from enterprise.domain_profiles import validate_domain_profile, profile_fingerprint
        profile = validate_domain_profile(snapshot['profile'])
        facts, scope, measurement = snapshot['facts'], snapshot['scope'], snapshot['measurement']
        if profile.get('schema_version') != 'manufacturing-domain/2' or snapshot['profile_id'] != profile['id']:
            _fail('冻结领域配置版本或标识不一致')
        if facts.get('schema_version') != 'manufacturing-facts/1' or facts.get('scope') != scope:
            _fail('冻结事实结构或范围不一致')
        if facts.get('provenance', {}).get('profile_sha256') != profile_fingerprint(profile):
            _fail('冻结事实与领域配置摘要不一致')
        if scope['home_factory'] != profile['factories']['home'] or scope['peer_factory'] != profile['factories']['peer']:
            _fail('冻结工厂与领域配置不一致')
        product = next((row for row in profile['products'] if row['id'] == scope['product_id']
                        and row['name'] == scope['product'] and row['specification'] == scope['specification']), None)
        if product is None or scope['industry'] != profile['industry']:
            _fail('冻结产品或行业不在领域配置中')
        expected = {'currency': profile['currency'], 'quantity_unit': product['reporting_unit'],
                    'unit_cost_unit': profile['currency'] + '/' + product['reporting_unit']}
        if measurement != expected or facts.get('measurement') != expected or facts.get('labels') != profile['labels']:
            _fail('冻结单位或要素名称与领域配置不一致')
        if snapshot['data_classification'] != profile['data_classification'] or facts.get('data_classification') != profile['data_classification']:
            _fail('冻结数据分类不一致')
        current_month = _month(scope['month'])
        if scope.get('previous_month') is not None and _month(scope['previous_month']) != current_month - 1:
            _fail('冻结前期不是连续可比期间')
        for row, factory, month in (
            (facts['current'], scope['home_factory'], scope['month']),
            (facts['benchmark']['home'], scope['home_factory'], scope['month']),
            (facts['benchmark']['peer'], scope['peer_factory'], scope['month']),
            (facts['budget']['baseline'], scope['home_factory'], scope['month']),
            (facts['budget']['current'], scope['home_factory'], scope['month']),
        ):
            _row_scope(row, scope, factory=factory, month=month, required=True)
            if row.get('currency') != expected['currency'] or row.get('unit') != expected['quantity_unit']:
                _fail('冻结事实记录单位不一致')
        _row_scope(facts['period'].get('baseline'), scope, factory=scope['home_factory'], month=scope.get('previous_month'))
        if facts['current'] != facts['benchmark']['home'] or facts['current'] != facts['budget']['current']:
            _fail('冻结当前事实不一致')
        for family in ('materials', 'labor', 'overhead'):
            pairs = [facts['details'][family]] if family == 'labor' else facts['details'][family]
            for pair in pairs:
                _row_scope(pair.get('current'), scope, factory=scope['home_factory'], month=scope['month'])
                _row_scope(pair.get('previous'), scope, factory=scope['home_factory'], month=scope.get('previous_month'))
            for row in facts.get('peer_details', {}).get(family, []):
                _row_scope(row, scope, factory=scope['peer_factory'], month=scope['month'])
        if set(snapshot['analyses']) != set(_MODES):
            _fail('冻结分析须包含归因与对标两个模式')
        for mode in _MODES:
            branch = snapshot['analyses'][mode]
            payload = branch['payload']
            if any(payload.get(key) != scope[key] for key in ('product', 'specification', 'month')):
                _fail('冻结叙事输入范围不一致')
            if payload.get('measurement') != expected or payload.get('source_fact_sha256') != _digest(facts):
                _fail('冻结叙事输入事实摘要或单位不一致')
            if profile_fingerprint(payload['domain_config']) != profile_fingerprint(profile):
                _fail('冻结叙事输入领域配置不一致')
            sources = {}
            for source in branch['sources']:
                ident = source.get('id')
                if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,127}', ident):
                    _fail('冻结来源标识无效')
                if ident in sources and sources[ident] != source:
                    _fail('冻结来源标识冲突')
                _source_scope(source, scope)
                sources[ident] = source
            narrative = branch['narrative']
            if not isinstance(narrative, dict) or not isinstance(narrative.get('sections'), list):
                _fail('冻结叙事缺少章节')
            refs = list(narrative.get('evidence_ids', []))
            for section in narrative['sections']:
                if section.get('element') not in ('材料', '人工', '制费'):
                    _fail('冻结叙事要素无效')
                refs.extend(section.get('evidence_ids', []))
            # Check explicit citation IDs, not arbitrary bracketed source text
            # inside provenance/quotations (which is untrusted data, not a marker).
            for rows in (narrative.get('industry_comparisons', []), narrative.get('yield_comparisons', [])):
                refs.extend(ref for row in rows for ref in row.get('evidence_ids', []))
            if any(ref not in sources for ref in refs):
                _fail('冻结叙事引用无法解析')
        for text in _strings(snapshot):
            if len(text) > 2_000_000 or any(ord(c) < 32 and c not in '\n\r\t' for c in text):
                _fail('冻结文本包含不支持的控制字符或超长内容')
        return snapshot
    except ManufacturingReportError:
        raise
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise ManufacturingReportError('冻结分析结构或语义校验失败：' + type(exc).__name__) from exc


def _leaves(source):
    if isinstance(source, dict) and source.get('records'):
        for row in source['records']:
            yield from _leaves(row)
    elif isinstance(source, dict):
        yield source


def _exact(value):
    if value is None:
        return '未提供／无定义'
    if isinstance(value, dict):
        return _exact(value.get('value'))
    if isinstance(value, bool) or not isinstance(value, str):
        _fail('核算表须使用冻结的精确十进制字符串')
    try:
        if not Decimal(value).is_finite():
            _fail('冻结数值不是有限十进制')
    except InvalidOperation:
        _fail('冻结数值格式无效')
    return value


def _reading_blocks(run):
    """Project layout only: do not regenerate explanations or calculate metrics."""
    scope, facts, measure = run['scope'], run['facts'], run['measurement']
    blocks = []
    def text(value, kind='text', level=1, mode=None):
        if value:
            blocks.append({'kind': kind, 'text': str(value), 'level': level, 'mode': mode})
    def table(headers, rows, weights=None):
        blocks.append({'kind': 'table', 'headers': headers, 'rows': rows, 'weights': weights})
    text('制造业成本分析阅读稿', 'title')
    text(scope['product'] + ' · ' + scope['specification'] + ' · ' + scope['month'])
    text(('SIMULATION／模拟数据，非真实经营业绩。' if run['data_classification'] == 'simulation'
          else '数据分类：' + run['data_classification'] + '。') +
         '待人工复核（' + run.get('review_status', 'needs_human_review') + '）；未签发。'
         '本DOCX/PDF仅为冻结JSON的阅读副本，不构成审批、任务创建、派发或已实现收益证明。')
    table(['字段', '冻结值'], [
        ['领域', run['profile']['label']], ['产品', scope['product']], ['规格', scope['specification']],
        ['本方工厂', scope['home_factory']], ['对标工厂', scope['peer_factory']],
        ['本期／可比前期', scope['month'] + '／' + (scope.get('previous_month') or '未提供')],
        ['金额／产出／单位成本单位', measure['currency'] + '／' + measure['quantity_unit'] + '／' + measure['unit_cost_unit']],
        ['冻结分析ID', run['analysis_run_id']], ['冻结时间', run['created']], ['JSON SHA-256', run['analysis_hash']],
    ], [1, 4])
    text('一、冻结核算事实', 'heading')
    text('下表原样呈现冻结的精确十进制值；未重新计算、换算或四舍五入。比率字段为冻结时标注的显示值，精确分子、分母及舍入规则仍以原JSON为准。')
    summaries = [('本期实际', facts['current']), ('前期实际', facts['period'].get('baseline')),
                 ('本期预算', facts['budget']['baseline']), ('对标方本期实际', facts['benchmark']['peer'])]
    table(['口径', '工厂／月份', '产出 (' + measure['quantity_unit'] + ')',
           '单位成本 (' + measure['unit_cost_unit'] + ')', '总额 (' + measure['currency'] + ')'],
          [[label, (row['factory'] + '\n' + row['month']) if row else '未提供',
            *[_exact(row.get(key)) if row else '未提供' for key in ('output', 'unitcost', 'total')]]
           for label, row in summaries], [1.3, 2.2, 1, 1.4, 1.3])
    for name, bridge in (('期间金额桥接', facts['period']), ('预算金额桥接', facts['budget'])):
        text(name, 'heading', 2)
        if not bridge.get('available'):
            text('完整比较依据未提供；不计算金额桥接。' + str(bridge.get('reason') or ''))
            continue
        table(['要素', '金额变动', '产量影响', '单位费用影响', '贡献 (%)'],
              [[facts['labels'][key], *[_exact(bridge['elements'][key].get(field))
                for field in ('amount_delta', 'volume_effect', 'unit_effect', 'contribution_pct')]] for key in _ELEMENTS])
        text('金额单位：' + measure['currency'] + '。该桥接为会计恒等分解，不是已核实业务原因。')
    text('同产量标准化对比', 'heading', 2)
    benchmark = facts['benchmark']
    table(['要素', '本方单位成本', '对标方单位成本', '单位差', '标准化金额差'],
          [[facts['labels'][key], *[_exact(benchmark['elements'][key][field])
            for field in ('home_unit', 'peer_unit', 'unit_gap', 'standardized_amount_gap')]] for key in _ELEMENTS])
    text('单位成本口径：' + measure['unit_cost_unit'] + '；金额口径：' + measure['currency'] +
         '；标准化产出：' + _exact(benchmark['standardized_output']) + ' ' + measure['quantity_unit'] +
         '；标准化总差：' + _exact(benchmark['standardized_amount_gap']) + ' ' + measure['currency'] +
         '。标准化差异不表示实际节约或效率优势。')
    for mode, heading in (('attribution', '二、期间成本归因'), ('benchmark', '三、同口径跨厂对标')):
        branch = run['analyses'][mode]
        narrative = branch['narrative']
        text(heading, 'heading')
        text('冻结生成状态：' + branch['generation_status'] + '；模型文字采用：' + ('是' if branch.get('used_llm') else '否') +
             '。检索降级：' + ('是' if run.get('retrieval_diagnostics', {}).get('degraded') else '否') + '。')
        text(narrative.get('overview'), mode=mode)
        for section in narrative['sections']:
            text(section.get('title') or section['element'], 'heading', 2)
            seen = set()
            bound_prose = section.get('prose') if section.get('prose_mode') == 'bound-numeric-prose/1' else None
            if bound_prose and not isinstance(bound_prose, str):
                _fail('冻结散文必须为文本')
            values = ((bound_prose,) if bound_prose else
                      (section.get('numeric_explanation') or section.get('fact'),
                       (section.get('model_core') or {}).get('hypothesis')))
            mechanism = section.get('mechanism_note')
            if mechanism and (not bound_prose or mechanism not in bound_prose):
                values += (mechanism,)
            for value in values:
                if value and value not in seen:
                    for paragraph in value.splitlines():
                        text(paragraph, mode=mode)
                    seen.add(value)
            if section.get('claim_type') == 'no_difference' and section.get('hypothesis'):
                text(section['hypothesis'], mode=mode)
            actions = []
            for value in (section.get('immediate_action'), section.get('accepted_model_recommendation') or
                          section.get('model_followup_action') or (section.get('model_core') or {}).get('recommendation')):
                if value and value not in actions:
                    actions.append(value)
            for value in actions:
                text(value, 'suggestion', mode=mode)
            gaps = list(dict.fromkeys(section.get('evidence_gaps') or section.get('missing_evidence') or []))
            if gaps:
                text('证据缺口（不阻断当前核算分析）：' + '；'.join(gaps) + '。', mode=mode)
            refs = list(dict.fromkeys(section.get('evidence_ids', [])))
            if refs:
                text('本节来源：' + ' '.join('[' + ref + ']' for ref in refs), mode=mode)
        for name, rows in (('行业参照（非业务原因）', narrative.get('industry_comparisons', [])),
                           ('已提供收率与适用标准对比', narrative.get('yield_comparisons', []))):
            if rows:
                text(name, 'heading', 2)
                for row in rows:
                    text(row['text'], mode=mode)
        text(narrative.get('followup_criteria'), mode=mode)
    return blocks


def _citation_catalog(run):
    """Original source IDs remain visible; display numbering is export-local only."""
    entries, mappings, positions = [], {}, {}
    for mode in _MODES:
        mappings[mode] = {}
        for source in run['analyses'][mode]['sources']:
            signature = _canonical({'id': source['id'], 'source': source.get('source'), 'text': source.get('text')})
            if signature not in positions:
                positions[signature] = len(entries) + 1
                entries.append({'number': len(entries) + 1, 'source': deepcopy(source), 'modes': [mode]})
            else:
                entries[positions[signature] - 1]['modes'].append(mode)
            mappings[mode][source['id']] = positions[signature]
    return entries, mappings


def _source_location(source):
    parts = []
    for row in _leaves(source.get('source') or {}):
        file = row.get('file') or row.get('source_id') or '来源名称未提供'
        location = str(file)
        if row.get('record_number') is not None or row.get('row_number') is not None:
            location += '；数据记录序号 ' + str(row.get('record_number', row.get('row_number'))) + '（不是推断物理行）'
        if row.get('line') is not None:
            location += '；原始物理行 ' + str(row['line'])
        if row.get('page') is not None:
            location += '；来源标注页 ' + str(row['page'])
        if row.get('offset') is not None:
            location += '；原文offset ' + str(row['offset'])
            if row.get('end_offset') is not None:
                location += '–' + str(row['end_offset'])
        if location not in parts:
            parts.append(location)
    return '\n'.join(parts)


def _appendix_blocks(run, entries):
    blocks = [{'kind': 'heading', 'text': '四、来源与冻结依据', 'level': 1}]
    def add(text, kind='text', level=1):
        blocks.append({'kind': kind, 'text': text, 'level': level})
    add('正文上标对应真实来源标识。下列页码、行号及offset来自冻结来源元数据，不是本阅读稿分页。'
        '未提供的来源位置不作推断；配置/BOM只描述参考语义，不证明本期实际事件。')
    records, record_ids = [], {}
    def register(record):
        signature = _canonical(record)
        if signature not in record_ids:
            record_ids[signature] = 'SRC-' + str(len(records) + 1).zfill(3)
            records.append((record_ids[signature], deepcopy(record)))
        return record_ids[signature]
    for entry in entries:
        source = entry['source']
        add('[' + str(entry['number']) + '] ' + source['id'], 'heading', 2)
        add('用途：' + '、'.join(entry['modes']) + '；类型：' + source.get('kind', '未标注') +
            '；引用只具有冻结记录注明的证明边界。')
        add(_source_location(source))
        if source.get('text'):
            add('冻结来源原文：' + source['text'])
        metadata = {key: value for key, value in source.items() if key not in ('source', 'text')}
        add('来源元数据：' + _canonical(metadata), 'audit')
        origin = source.get('source') or {}
        leaves = list(_leaves(origin))
        ids = [register(row) for row in leaves]
        if ids:
            add('原始来源记录：' + '、'.join(ids))
        container = {key: value for key, value in origin.items() if key != 'records'} if origin.get('records') else {}
        if container:
            add('聚合来源元数据：' + _canonical(container), 'audit')
    add('核算摘要与预算来源', 'heading', 2)
    for name, row in (('本期实际', run['facts']['current']), ('前期实际', run['facts']['period'].get('baseline')),
                      ('本期预算', run['facts']['budget']['baseline']), ('对标方本期', run['facts']['benchmark']['peer'])):
        if row:
            add(name + '：' + register(row['source']))
    add('原始记录定位与精确字段', 'heading', 2)
    for identifier, record in records:
        add(identifier, 'heading', 3)
        # Full original columns/fields, SHA, row ordinal and canonical provenance;
        # never read the path printed here or guess rows from a newer source.
        for key, value in record.items():
            add(key + '：' + (value if isinstance(value, str) else _canonical(value)), 'audit')
    add('冻结归档说明', 'heading', 2)
    add('JSON为审计权威原件；本稿仅显示已冻结内容，不代表再次检索、模型重算、人工批准或任务派发。'
        '阅读稿版本：' + EXPORT_VERSION + '；JSON SHA-256：' + run['analysis_hash'] + '。')
    return blocks


def _font(blocks):
    # Existing deployment-approved font resolver. The frozen run cannot specify
    # a font filename/path; only the operator's existing deployment selection is read.
    from report.export import font_descriptor
    try:
        descriptor = font_descriptor('\n'.join(_strings(blocks)))
        path = Path(descriptor['path'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != descriptor['sha256']:
            raise ValueError('font hash changed')
        # Only known sibling faces of the approved regular font are considered;
        # no caller path or source filename participates in font selection.
        bold_name = {'HarmonyOS_Sans_SC_Regular.ttf': 'HarmonyOS_Sans_SC_Bold.ttf',
                     'Deng.ttf': 'Dengb.ttf', 'msyh.ttc': 'msyhbd.ttc'}.get(path.name)
        if bold_name:
            bold_path = path.with_name(bold_name)
            if bold_path.is_file():
                from reportlab.pdfbase.ttfonts import TTFont
                face = TTFont('ManufacturingBoldProbe', str(bold_path), subfontIndex=0)
                if all(character.isspace() or ord(character) in face.face.charToGlyph
                       for character in '\n'.join(_strings(blocks))):
                    descriptor['bold_path'] = str(bold_path)
                    descriptor['bold_sha256'] = hashlib.sha256(bold_path.read_bytes()).hexdigest()
        return descriptor
    except (RuntimeError, OSError, KeyError, ValueError) as exc:
        raise RuntimeError('制造业阅读稿导出缺少可用且覆盖文本的已授权中文字体；请由部署管理员配置字体。') from exc


def _docx(run, blocks, entries, mappings, font):
    from docx import Document
    from docx.oxml import OxmlElement, parse_xml
    from docx.oxml.ns import qn
    from docx.opc.part import Part
    from docx.opc.packuri import PackURI
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.shared import Cm, Pt, RGBColor
    from lxml.etree import tostring
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(1.8)
    section.left_margin = section.right_margin = Cm(1.9)
    for name in ('Normal', 'Title', 'Heading 1', 'Heading 2', 'Heading 3'):
        style = document.styles[name]
        style.font.name = font['family']
        style.font.color.rgb = RGBColor.from_string(BLACK)
        style.font.size = Pt({'Title': 21, 'Heading 1': 14, 'Heading 2': 11, 'Heading 3': 10}.get(name, 10))
        style._element.get_or_add_rPr().get_or_add_rFonts().set(qn('w:eastAsia'), font['family'])
        style.paragraph_format.space_after = Pt(6)
        if name != 'Normal':
            style.font.bold = True
            style.paragraph_format.keep_with_next = True
    document.styles['Normal'].paragraph_format.line_spacing = 1.2
    document.core_properties.title = '制造业成本分析阅读稿'
    document.core_properties.subject = '冻结JSON ' + run['analysis_hash'] + '；未签发阅读副本'
    document.core_properties.author = '成本分析系统'
    footnotes = parse_xml('<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
    for number, footnote_type, child in ((-1, 'separator', 'separator'), (0, 'continuationSeparator', 'continuationSeparator')):
        note = OxmlElement('w:footnote'); note.set(qn('w:id'), str(number)); note.set(qn('w:type'), footnote_type)
        p = OxmlElement('w:p'); r = OxmlElement('w:r'); r.append(OxmlElement('w:' + child)); p.append(r); note.append(p); footnotes.append(note)
    for entry in entries:
        note = OxmlElement('w:footnote'); note.set(qn('w:id'), str(entry['number']))
        p = OxmlElement('w:p'); r = OxmlElement('w:r'); ref = OxmlElement('w:footnoteRef'); r.append(ref); p.append(r)
        r = OxmlElement('w:r'); props = OxmlElement('w:rPr'); fonts = OxmlElement('w:rFonts')
        fonts.set(qn('w:ascii'), font['family']); fonts.set(qn('w:eastAsia'), font['family']); props.append(fonts)
        size = OxmlElement('w:sz'); size.set(qn('w:val'), '16'); props.append(size); r.append(props)
        t = OxmlElement('w:t'); t.text = ' ' + entry['source']['id'] + '；' + _source_location(entry['source']); r.append(t); p.append(r); note.append(p); footnotes.append(note)
    part = Part(PackURI('/word/footnotes.xml'), 'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml',
                tostring(footnotes, xml_declaration=True, encoding='UTF-8', standalone=True), document.part.package)
    document.part.relate_to(part, RT.FOOTNOTES)
    def runs(paragraph, value, mode=None):
        start = 0
        for match in _REF.finditer(value):
            number = mappings.get(mode, {}).get(match[1])
            if not number:
                continue
            paragraph.add_run(value[start:match.start()])
            run = paragraph.add_run(); ref = OxmlElement('w:footnoteReference'); ref.set(qn('w:id'), str(number)); run._r.append(ref)
            start = match.end()
        paragraph.add_run(value[start:])
    for block in blocks:
        kind = block['kind']
        if kind == 'table':
            table = document.add_table(rows=1, cols=len(block['headers']))
            table.style = 'Table Grid'; table.autofit = False
            weights = block.get('weights') or [1] * len(block['headers'])
            widths = [17.2 * weight / sum(weights) for weight in weights]
            for cell, title, width in zip(table.rows[0].cells, block['headers'], widths):
                cell.width = Cm(width); cell.text = title
                for run_ in cell.paragraphs[0].runs:
                    run_.bold = True
            repeat = OxmlElement('w:tblHeader'); table.rows[0]._tr.get_or_add_trPr().append(repeat)
            for values in block['rows']:
                cells = table.add_row().cells
                for cell, value, width in zip(cells, values, widths):
                    cell.width = Cm(width); cell.text = str(value)
            for row in table.rows:
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        paragraph.paragraph_format.space_after = Pt(3)
                        for run_ in paragraph.runs:
                            run_.font.size = Pt(8)
            document.add_paragraph().paragraph_format.space_after = Pt(0)
        elif kind in ('heading', 'title'):
            paragraph = document.add_paragraph(style='Title' if kind == 'title' else 'Heading ' + str(block.get('level', 1)))
            runs(paragraph, block['text'], block.get('mode'))
        else:
            paragraph = document.add_paragraph()
            if kind == 'suggestion':
                paragraph.paragraph_format.left_indent = Cm(0.35)
                borders = OxmlElement('w:pBdr'); left = OxmlElement('w:left')
                for key, value in (('val', 'single'), ('sz', '12'), ('space', '4'), ('color', BLUE)):
                    left.set(qn('w:' + key), value)
                borders.append(left); paragraph._p.get_or_add_pPr().append(borders)
                prefix = paragraph.add_run('建议'); prefix.bold = True; prefix.font.color.rgb = RGBColor.from_string(BLUE)
                if not block['text'].startswith('建议'):
                    paragraph.add_run('：')
            value = block['text'][2:] if kind == 'suggestion' and block['text'].startswith('建议') else block['text']
            runs(paragraph, value, block.get('mode'))
            if kind == 'audit':
                paragraph.paragraph_format.space_after = Pt(3)
                for run_ in paragraph.runs:
                    run_.font.size = Pt(8)
            # Permit wrapped long identifiers without inserting characters into
            # source names, decimal values or their searchable original text.
            wrap = OxmlElement('w:wordWrap'); wrap.set(qn('w:val'), '1'); paragraph._p.get_or_add_pPr().append(wrap)
    footer = section.footer.paragraphs[0]
    footer.alignment = 2
    footer.add_run('未签发阅读副本 · ')
    field = OxmlElement('w:fldSimple'); field.set(qn('w:instr'), 'PAGE'); footer._p.append(field)
    result = BytesIO(); document.save(result); return result.getvalue()


def _pdf(run, blocks, entries, mappings, font):
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    class StrongParagraph(Paragraph):
        """Readable native text with synthetic bold when only regular CJK exists."""
        def draw(self):
            canvas = self.canv
            make_text = canvas.beginText
            def bold_text(*args, **kwargs):
                obj = make_text(*args, **kwargs)
                obj.setTextRenderMode(2)
                return obj
            canvas.saveState()
            canvas.setLineWidth(0.22)
            canvas.beginText = bold_text
            try:
                super().draw()
            finally:
                canvas.beginText = make_text
                canvas.restoreState()
    name = 'ManufacturingReading_' + font['sha256'][:12]
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(name, font['path'], subfontIndex=font.get('subfont_index', 0)))
    bold_name = name
    if font.get('bold_path'):
        bold_name = name + '_Bold'
        if bold_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(bold_name, font['bold_path'], subfontIndex=0))
    pdfmetrics.registerFontFamily(name, normal=name, bold=bold_name, italic=name, boldItalic=bold_name)
    base = ParagraphStyle('MfgBody', fontName=name, fontSize=10, leading=14, textColor=colors.HexColor('#' + BLACK),
                          spaceAfter=6, alignment=TA_LEFT, splitLongWords=True, wordWrap='CJK', allowWidows=0, allowOrphans=0)
    # When no vetted bold sibling exists, render only the short label with
    # stroke-and-fill via a local text-object override, without duplicating text.
    class AdviceParagraph(Paragraph):
        def draw(self):
            if bold_name != name:
                return super().draw()
            canvas, original = self.canv, self.canv.beginText
            def begin_text(*args, **kwargs):
                obj = original(*args, **kwargs)
                output = obj._textOut
                def marked_output(value, *output_args, **output_kwargs):
                    obj.setTextRenderMode(2 if value == '建议' else 0)
                    return output(value, *output_args, **output_kwargs)
                obj._textOut = marked_output
                return obj
            canvas.saveState(); canvas.setLineWidth(0.22); canvas.beginText = begin_text
            try:
                return super().draw()
            finally:
                canvas.beginText = original; canvas.restoreState()
    styles = {'text': base, 'audit': ParagraphStyle('MfgAudit', parent=base, fontSize=7.5, leading=10.5, spaceAfter=3),
              'title': ParagraphStyle('MfgTitle', parent=base, fontSize=21, leading=27, spaceAfter=12, keepWithNext=True)}
    for level, size in ((1, 14), (2, 11), (3, 10)):
        styles['heading' + str(level)] = ParagraphStyle('MfgH' + str(level), parent=base, fontSize=size,
            leading=size + 5, spaceBefore=10, spaceAfter=5, keepWithNext=True)
    suggestion = ParagraphStyle('MfgAdvice', parent=base, leftIndent=10, spaceBefore=4, spaceAfter=6)
    class Advice(Flowable):
        def __init__(self, paragraph):
            Flowable.__init__(self); self.paragraph = paragraph
        def wrap(self, available_width, available_height):
            self.width, self.height = self.paragraph.wrap(available_width - 4, available_height)
            return available_width, self.height
        def split(self, available_width, available_height):
            return [Advice(item) for item in self.paragraph.split(available_width - 4, available_height)]
        def draw(self):
            self.canv.setStrokeColor(colors.HexColor('#' + BLUE)); self.canv.setLineWidth(1.5)
            self.canv.line(0, 0, 0, self.height)
            self.paragraph.drawOn(self.canv, 4, 0)

    def markup(value, mode=None):
        chunks, start = [], 0
        for match in _REF.finditer(value):
            number = mappings.get(mode, {}).get(match[1])
            if not number:
                continue
            chunks.append(escape(value[start:match.start()])); chunks.append('<super>' + str(number) + '</super>')
            start = match.end()
        chunks.append(escape(value[start:])); return ''.join(chunks).replace('\n', '<br/>')
    story = []
    width = A4[0] - 3.8 * cm
    cell_style = ParagraphStyle('MfgCell', parent=base, fontSize=8, leading=11, spaceAfter=0)
    for block in blocks:
        kind = block['kind']
        if kind == 'table':
            values = [[Paragraph(escape(str(value)).replace('\n', '<br/>'), cell_style) for value in row]
                      for row in [block['headers'], *block['rows']]]
            weights = block.get('weights') or [1] * len(block['headers'])
            table = Table(values, colWidths=[width * weight / sum(weights) for weight in weights], repeatRows=1, hAlign='LEFT')
            table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F1F3F5')),
                                      ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#CCD1D6')),
                                      ('LEFTPADDING', (0, 0), (-1, -1), 5), ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                                      ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 5)]))
            story.extend((table, Spacer(1, 7)))
        elif kind == 'suggestion':
            advice = block['text'][2:] if block['text'].startswith('建议') else '：' + block['text']
            story.append(Advice(AdviceParagraph('<font color="#' + BLUE + '"><b>建议</b></font>' + markup(advice, block.get('mode')), suggestion)))
        else:
            style = styles['heading' + str(block.get('level', 1))] if kind == 'heading' else styles.get(kind, base)
            value = markup(block['text'], block.get('mode'))
            if kind in ('heading', 'title'):
                value = '<b>' + value + '</b>'
            paragraph_type = StrongParagraph if kind in ('heading', 'title') and bold_name == name else Paragraph
            story.append(paragraph_type(value, style))
    result = BytesIO()
    document = SimpleDocTemplate(result, pagesize=A4, leftMargin=1.9 * cm, rightMargin=1.9 * cm,
        topMargin=1.8 * cm, bottomMargin=1.8 * cm, title='制造业成本分析阅读稿',
        author='成本分析系统', subject='冻结JSON ' + run['analysis_hash'] + '；未签发阅读副本')
    def footer(canvas, doc):
        canvas.saveState(); canvas.setFont(name, 8); canvas.setFillColor(colors.HexColor('#555555'))
        canvas.drawRightString(A4[0] - 1.9 * cm, 0.9 * cm, '未签发阅读副本 · ' + str(doc.page)); canvas.restoreState()
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return result.getvalue()


def export_manufacturing_report(frozen_run, format):
    """Return a native editable DOCX or searchable PDF from one frozen snapshot.

    Call after ManufacturingService.get_run authorization. The only filesystem
    read here is the deployment-controlled font selected by font_descriptor.
    No exporter argument is a path, and source paths are printed, never opened.
    """
    if format not in ('docx', 'pdf'):
        raise ManufacturingReportError('制造业阅读稿仅支持docx或pdf；JSON原件由授权分析接口提供')
    run = validate_manufacturing_run(frozen_run)
    blocks = _reading_blocks(run)
    entries, mappings = _citation_catalog(run)
    blocks.extend(_appendix_blocks(run, entries))
    font = _font(blocks)
    with _LOCK:
        return _docx(run, blocks, entries, mappings, font) if format == 'docx' else _pdf(run, blocks, entries, mappings, font)


__all__ = ['export_manufacturing_report', 'validate_manufacturing_run', 'ManufacturingReportError']

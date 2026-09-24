"""Controlled domain entity/relation graph for formal knowledge releases.

Entities are extracted from confirmed source text. Semantic relations require
explicit recipe tables, process headings, flow arrows, equipment-use predicates
or controlled metric rows. Other nearby mentions are labelled co_occurs and never
used for semantic expansion. Exact raw quotes/offsets and contextual heading
proofs are retained; no relation proves an actual operating event or causality.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re
import unicodedata
from typing import Iterable

from enterprise.knowledge_applicability import matching_view, source_sections
from enterprise.domain_vocabulary import (
    VOCABULARY_SCHEMA, ENTITY_TYPES, explicit_other_domain,
    vocabulary_fingerprint, vocabulary_from_metadata,
)


DOMAIN_GRAPH_V2 = {
    'name': 'controlled-domain-entity-relation',
    'version': 2,
    'entity_types': ['product', 'material', 'process', 'equipment', 'metric'],
    'relation_types': [
        'product_contains_material',
        'material_undergoes_process',
        'process_uses_equipment',
        'process_has_controlled_metric',
        'entity_co_occurs_in_chunk',
    ],
    'evidence': 'explicit_template_exact_quote_or_labelled_cooccurrence',
    'semantic_boundary': 'semantic edges require direct source template; co-occurrence edges are retrieval-only and never causal',
    'offset_unit': 'unicode_character',
    'offset_end': 'exclusive',
}

# V2 is retained verbatim for readers of immutable historical releases. New
# publications advertise V3; loading a stored graph never rebuilds its edges.
DOMAIN_GRAPH_V3 = {
    **DOMAIN_GRAPH_V2,
    'version': 3,
    'relation_types': [*DOMAIN_GRAPH_V2['relation_types'], 'process_precedes_process'],
    'process_flow': 'explicit_text_arrows_only; no OCR or inferred step order',
}
# Preserve the historical default for unconfigured pharma releases. Explicit
# reviewed cross-industry vocabularies use V4, never relabel an old artifact.
DOMAIN_GRAPH = DOMAIN_GRAPH_V3
DOMAIN_GRAPH_V4 = {
    **DOMAIN_GRAPH_V3,
    'version': 4,
    'vocabulary': VOCABULARY_SCHEMA,
    'configuration_boundary': 'reviewed matching vocabulary only; no scope grants or inferred edges',
    'metric_binding': 'explicit configured pair AND direct source predicate/threshold',
}

# Aliases are matching vocabulary only.  The persisted quote always comes from
# the original confirmed text, including compatibility characters and line breaks.
ENTITY_ALIASES = {
    'product': {
        '银黄口服液': ('银黄口服液', '银⻩口服液', '银⻩⼝服液'),
        '板蓝根颗粒': ('板蓝根颗粒', '板蓝根颗粒剂'),
        '六味地黄胶囊': ('六味地黄胶囊', '六味地⻩胶囊', '六味地⻩胶囊剂'),
    },
    'material': {
        '金银花': ('金银花',),
        '黄芩提取物': ('黄芩提取物',),
        '黄芩': ('黄芩',),
        '板蓝根': ('板蓝根',),
        '熟地黄': ('熟地黄', '熟地⻩'),
        '山茱萸': ('山茱萸',),
        '山药': ('山药',),
        '泽泻': ('泽泻',),
        '茯苓': ('茯苓',),
        '牡丹皮': ('牡丹皮',),
        '蔗糖': ('蔗糖',),
        '苯甲酸钠': ('苯甲酸钠',),
        '纯化水': ('纯化水',),
        '糊精': ('糊精',),
        '空心胶囊': ('空心胶囊', '空⼼胶囊'),
        '包装材料': ('包装材料',),
    },
    'process': {
        '提取': ('提取', '水提取', '⽔提取'),
        '浓缩': ('浓缩',),
        '配制': ('配制',),
        '过滤': ('过滤',),
        '灌装': ('灌装',),
        '灭菌': ('灭菌',),
        '灯检': ('灯检',),
        '包装': ('包装', '外包装', '内包装'),
        '制粒': ('制粒',),
        '干燥': ('干燥', '⼲燥'),
        '分装': ('分装',),
        '混合': ('混合', '总混'),
        '清洗': ('清洗',),
        '拣选': ('拣选',),
        '粉碎': ('粉碎', '分别粉碎'),
        '过筛': ('过筛',),
        '整粒': ('整粒',),
        '填充': ('填充', '胶囊填充'),
        '抛光': ('抛光',),
        '折旧': ('折旧', '折旧费', '折旧计提'),
        '维修': ('维修', '维护'),
    },
    'metric': {
        '提取收率': ('提取收率',),
        '浓缩损耗': ('浓缩损耗',),
        '灭菌合格率': ('灭菌合格率',),
        '制粒收率': ('制粒收率',),
        '干燥水分': ('水分', '干燥水分'),
        '装量合格率': ('装量合格率',),
        '粉碎收率': ('粉碎收率',),
        '混合均匀度': ('混合均匀度',),
        '填充合格率': ('填充合格率',),
        '密封合格率': ('密封合格率',),
        '灌装损耗': ('灌装损耗',),
    },
    'equipment': {
        '多功能提取罐': ('多功能提取罐',),
        '双效浓缩器': ('双效浓缩器',),
        '三效浓缩器': ('三效浓缩器',),
        '浓配罐': ('浓配罐',),
        '稀配罐': ('稀配罐',),
        '灌封一体机': ('灌封一体机', '口服液灌封一体机'),
        '湿热灭菌柜': ('湿热灭菌柜',),
        '灯检机': ('灯检机',),
        '湿法制粒机': ('湿法制粒机',),
        '流化床干燥机': ('流化床干燥机',),
        '整粒机': ('整粒机',),
        '三维混合机': ('三维混合机',),
        '颗粒分装机': ('颗粒分装机',),
        '中药材清洗机': ('中药材清洗机',),
        '热风循环烘箱': ('热风循环烘箱',),
        '万能粉碎机': ('万能粉碎机',),
        '振荡筛': ('振荡筛',),
        '全自动胶囊填充机': ('全自动胶囊填充机',),
        '胶囊抛光机': ('胶囊抛光机',),
        '铝塑泡罩包装机': ('铝塑泡罩包装机',),
        '纯化水系统': ('纯化水系统',),
        # Equipment identifiers are controlled aliases and are matched only when
        # the source actually contains the identifier.
        'EQ-TQ-001': ('EQ-TQ-001', 'EQ-\nTQ-\n001'),
        'EQ-KL-001': ('EQ-KL-001', 'EQ-\nKL-\n001'),
        'NJP-3200': ('NJP-3200', 'NJP-\n3200'),
        'DXDK-40VI': ('DXDK-40VI', 'DXDK-\n40VI'),
    },
}

COOCCURRENCE_RULES = (
    ('product', 'material'), ('product', 'process'), ('product', 'equipment'),
    ('product', 'metric'), ('material', 'process'), ('material', 'equipment'),
    ('material', 'metric'), ('process', 'equipment'), ('process', 'metric'),
    ('equipment', 'metric'),
)

SEMANTIC_TYPES = {
    'product_contains_material': ('product', 'material'),
    'material_undergoes_process': ('material', 'process'),
    'process_uses_equipment': ('process', 'equipment'),
    'process_has_controlled_metric': ('process', 'metric'),
    'process_precedes_process': ('process', 'process'),
}


# Parse physical lines, not whitespace-normalized document streams: removing
# newlines first would invent links across columns, pages and section headings.
_RIGHT_ARROW = re.compile(r'→|->|⇒')
_ANY_ARROW = re.compile(r'→|->|⇒|←|<-|⇐|↓|↑')


def _flow_node(value: str, start: int, aliases: dict):
    """An entire flow cell must be a process, never a substring of equipment."""
    raw = value.strip()
    leading = len(value) - len(value.lstrip())
    # Parameters are opaque annotations; their equipment/process names are not
    # additional route nodes. A malformed/multiline parenthesis fails closed.
    view = _without_whitespace(matching_view(raw))
    label = re.sub(r'\([^()]*\)$', '', view)
    candidates = []
    for name, values in aliases['process'].items():
        for alias in values:
            normalized = _without_whitespace(matching_view(alias))
            if label == normalized:
                candidates.append((len(normalized), name, alias))
    if not candidates:
        return None
    _, name, alias = max(candidates)
    return {'entity_id': _stable_id('process', name), 'entity_type': 'process',
            'name': name, 'alias': alias, 'start': start + leading,
            'end': start + leading + len(raw)}


def _flow_sections(row):
    meta = row.get('meta') or {}
    sections = source_sections(row['text'], meta)
    # A window may start after the heading. Inherit ONLY an explicit section
    # proof from the chunker, not the multi-product authorization list.
    applicability = meta.get('applicability') or {}
    if applicability.get('kind') == 'product' and len(applicability.get('products', [])) == 1:
        for section in sections:
            if not section['section'] and section['basis'] == 'no_product_section':
                section.update(kind='product', products=list(applicability['products']),
                               section=applicability.get('section', meta.get('section', '')),
                               basis='inherited_chunk_product_section')
    return sections


def _text_flow_relations(row, aliases):
    """Return explicit, contiguous text links and inspectable skipped arrows.

    Straight ↓ is supported only between two complete single-node lines, with
    intervening whitespace/│ lines and exactly one ↓. Bent/merged branches,
    left arrows and disconnected or unknown labels are deliberately unsupported.
    No route template, numbered step, proximity rule or page reordering is used.
    """
    text = row['text']
    relations, diagnostics = [], []
    for section in _flow_sections(row):
        start, end = section['offset'], section['end_offset']
        section_text = text[start:end]
        if not _ANY_ARROW.search(section_text):
            continue
        products = section.get('products', [])
        if section['kind'] != 'product' or len(products) != 1:
            diagnostics.append({'reason': 'ambiguous_product_section', 'start': start, 'end': end})
            continue
        lines, offset = [], start
        for line in section_text.splitlines(keepends=True):
            lines.append((offset, line.rstrip('\r\n')))
            offset += len(line)
        accepted_arrows = set()
        for line_start, line in lines:
            # A left arrow in a merge cell cannot be flattened into a right
            # arrow chain. Reject this physical line, but downstream vertical
            # links may still use a uniquely resolved destination cell.
            if re.search(r'←|<-|⇐', line):
                continue
            arrows = list(_RIGHT_ARROW.finditer(line))
            for number, arrow in enumerate(arrows):
                left_boundary = arrows[number - 1].end() if number else 0
                right_boundary = arrows[number + 1].start() if number + 1 < len(arrows) else len(line)
                left_raw = line[left_boundary:arrow.start()]
                # Horizontal shaft belongs to the arrow, not the node.
                left_raw = re.sub(r'[─━-]+\s*$', '', left_raw)
                right_raw = line[arrow.end():right_boundary]
                right_raw = re.sub(r'[─━-]+\s*$', '', right_raw)
                source = _flow_node(left_raw, line_start + left_boundary, aliases)
                target = _flow_node(right_raw, line_start + arrow.end(), aliases)
                if source and target and source['entity_id'] != target['entity_id']:
                    relations.append((source, target, section, 'explicit_right_arrow_text'))
                    accepted_arrows.add(line_start + arrow.start())
        # Recover a single destination node on the preceding line, including
        # an explicit incoming merge arrow. No predecessors of that merge are
        # guessed. The intervening vertical shaft must align with this node.
        for number, (arrow_start, arrow_line) in enumerate(lines):
            if arrow_line.strip() != '↓':
                continue
            column = arrow_line.index('↓')
            before, after = number - 1, number + 1
            while before >= 0 and lines[before][1].strip() == '│':
                if lines[before][1].index('│') != column:
                    break
                before -= 1
            while after < len(lines) and lines[after][1].strip() == '│':
                if lines[after][1].index('│') != column:
                    break
                after += 1
            if before < 0 or after >= len(lines):
                continue
            source_start, source_line = lines[before]
            target_start, target_line = lines[after]
            if not source_line.strip() or not target_line.strip():
                continue
            # Target is a standalone node, not a second branch or a table row.
            target = _flow_node(target_line, target_start, aliases)
            candidates = []
            # Split at arrow/branch glyphs. Match only complete process cells.
            boundaries = list(re.finditer(r'→|->|⇒|←|<-|⇐|[┐┘┌└├┤┬┴┼│]', source_line))
            stops = [0] + [m.end() for m in boundaries]
            ends = [m.start() for m in boundaries] + [len(source_line)]
            for left, right in zip(stops, ends):
                raw = source_line[left:right]
                raw = re.sub(r'[─━-]+\s*$', '', raw)
                node = _flow_node(raw, source_start + left, aliases)
                if node:
                    candidates.append(node)
            if len(candidates) != 1 or not target:
                continue
            source = candidates[0]
            # The chosen process must be the outgoing node, not an earlier node
            # in a horizontal path ending at an unknown/material/equipment label.
            tail = source_line[source['end'] - source_start:]
            if not re.fullmatch(r'\s*(?:(?:←|<-|⇐)[─━-]*[┘┐]?)?\s*', tail):
                continue
            # For unindented one-column text, column zero is unambiguous. For
            # PDF diagrams require the shaft to fall inside both physical cells.
            def aligned(node, line_start, line):
                left, right = node['start'] - line_start, node['end'] - line_start
                def width(value):
                    return sum(2 if unicodedata.east_asian_width(c) in {'W', 'F'} else 1 for c in value)
                return (left <= column < right) or (
                    width(line[:left]) <= column < width(line[:right]))
            if not aligned(source, source_start, source_line) or not aligned(target, target_start, target_line):
                continue
            if source['entity_id'] == target['entity_id']:
                continue  # e.g. 内包装/外包装 collapse under the existing alias vocabulary
            relations.append((source, target, section, 'explicit_vertical_arrow_text'))
            accepted_arrows.add(arrow_start + column)
        for arrow in _ANY_ARROW.finditer(section_text):
            absolute = start + arrow.start()
            if absolute not in accepted_arrows:
                reason = ('unsupported_left_arrow_layout' if arrow.group() in {'←', '<-', '⇐'}
                          else 'unsupported_or_ambiguous_text_flow')
                diagnostics.append({'reason': reason, 'start': absolute, 'end': start + arrow.end(),
                                    'products': list(products)})
    return relations, diagnostics



def _stable_id(entity_type: str, name: str) -> str:
    value = json.dumps([entity_type, name], ensure_ascii=False, separators=(',', ':'))
    return 'ent_' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def _relation_id(source_id: str, target_id: str, relation_type: str, chunk_id: str, start: int, end: int) -> str:
    value = json.dumps([source_id, target_id, relation_type, chunk_id, start, end], ensure_ascii=False, separators=(',', ':'))
    return 'rel_' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:28]


def _without_whitespace(value: str) -> str:
    return re.sub(r'\s+', '', value)


def _normalized_with_map(text: str):
    """Return NFKC matching text and a map to raw character indices.

    NFKC can expand one raw character into multiple matching characters.  Every
    expanded character maps back to its raw source index, so a match can be
    converted to an exact raw half-open span without persisting normalized offsets.
    """
    normalized, raw_indices = [], []
    for raw_index, char in enumerate(text):
        value = matching_view(char)
        for item in value:
            if item.isspace():
                continue
            normalized.append(item)
            raw_indices.append(raw_index)
    return ''.join(normalized), raw_indices


def _alias_spans(text: str, alias: str):
    view, raw_indices = _normalized_with_map(text)
    needle = _without_whitespace(matching_view(alias))
    if not needle:
        return []
    spans = []
    offset = 0
    while True:
        index = view.find(needle, offset)
        if index < 0:
            break
        end_index = index + len(needle)
        if end_index <= len(raw_indices):
            start = raw_indices[index]
            end = raw_indices[end_index - 1] + 1
            spans.append((start, end))
        offset = max(index + 1, end_index)
    return spans


def _source_for_span(row: dict, local_start: int, local_end: int) -> dict:
    meta = row['meta']
    absolute_start = int(meta.get('offset', 0)) + local_start
    absolute_end = int(meta.get('offset', 0)) + local_end
    page_matches = [span for span in (meta.get('page_spans') or [])
                    if span.get('offset', 0) < absolute_end and absolute_start < span.get('end_offset', 0)]
    pages = list(dict.fromkeys(int(span['page']) for span in page_matches if 'page' in span))
    vocabulary = vocabulary_from_metadata(meta)
    vocabulary_source = ({'graph_vocabulary_version': VOCABULARY_SCHEMA,
                          'graph_vocabulary_fingerprint': vocabulary_fingerprint(vocabulary),
                          'source_config_fingerprint': vocabulary['source_config_fingerprint']}
                         if vocabulary is not None else {})
    return {
        **vocabulary_source,
        'document_id': meta.get('doc_id'),
        'version_id': row.get('version_id'),
        'chunk_id': row.get('chunk_id'),
        'filename': meta.get('filename'),
        'document_sha256': meta.get('sha256'),
        'offset': absolute_start,
        'end_offset': absolute_end,
        'page': pages[0] if pages else None,
        'pages': pages,
        'page_spans': page_matches,
        'section': meta.get('section', ''),
        'scope_products': list(meta.get('scope_products') or []),
        'scope_factories': list(meta.get('scope_factories') or []),
        'applicability': meta.get('applicability', {}),
        'effective_from': meta.get('effective_from'),
        'effective_to': meta.get('effective_to'),
        'confirmed_at': meta.get('confirmed_at'),
    }


def _near(text: str, left: dict, right: dict, limit: int = 220):
    return abs(left['start'] - right['start']) <= limit


def _matching_excerpt(text: str, start: int, end: int, *, before: int = 60, after: int = 100):
    """Return a raw excerpt that includes the direct source predicate/value."""
    left = max(0, start - before)
    right = min(len(text), end + after)
    return left, right


def _configured_semantic_relation(source, target, text, vocabulary):
    """V4: configured pairs are disambiguators, never sufficient evidence."""
    pair = (source['entity_type'], target['entity_type'])
    if source['end'] > target['start']:
        return None  # a process substring inside a metric/equipment isn't a row
    between_raw = text[source['end']:target['start']]
    between = _without_whitespace(matching_view(between_raw))
    if len(between) > 180 or re.search(r'[。；;]|禁止|无关|不得|不含|不使用|不采用|未使用|并非|不是', between):
        return None
    prefix = matching_view(text[max(0, source['start'] - 12):source['start']])
    if re.search(r'(?:禁止|不得|无需|不使用|不采用|未使用|并非|不是)\s*$', prefix):
        return None
    suffix = matching_view(text[target['end']:target['end'] + 80])
    if pair == ('product', 'material'):
        if re.fullmatch(r'(?:由|包含|含有|使用|采用)(?:原料|材料|物料)?[:：]?', between):
            return 'product_contains_material', 'explicit_material_composition_predicate'
        labels = r'BOM|物料清单|材料清单|原料名称|材料名称|物料名称|原材料|处方组成|处方量|配方'
        if re.fullmatch(r'[:：|,，]*(?:' + labels + r')[:：|,，]*(?:(?:原料|材料|物料)名称[:：|,，]*)?',
                        between, re.I) and re.match(r'\s*[|,，\t:：]*\s*\d+(?:\.\d+)?', suffix):
            return 'product_contains_material', 'explicit_bom_material_quantity_row'
    elif pair == ('material', 'process'):
        if re.fullmatch(r'(?:(?:→|->|⇒)|(?:经|经过|进入|采用|进行))', between):
            return 'material_undergoes_process', 'direct_flow_arrow_or_predicate'
    elif pair == ('process', 'equipment'):
        if re.fullmatch(r'(?:工序)?(?:使用|采用|进入|通过)(?:设备)?[:：]?', between):
            return 'process_uses_equipment', 'explicit_equipment_use_predicate'
        if re.fullmatch(r'\([^()。；;\n]{0,40}', between) and ')' in suffix:
            return 'process_uses_equipment', 'equipment_inside_process_parenthesis'
    elif pair == ('process', 'metric'):
        bindings = {(item['process'], item['metric']) for item in vocabulary['process_metric_bindings']}
        if (source['name'], target['name']) not in bindings:
            return None
        # Only a direct adjacent table cell or an explicit named predicate. A
        # metric's spelling never implies its process; unrelated prose and a
        # second process between the endpoints fail closed.
        if not re.fullmatch(r'(?:工序)?(?:的|控制指标|控制参数|指标|参数|要求|控制|规定)?[:：|,，]*', between):
            return None
        if between_raw.count('\n') > 1:
            return None
        threshold = r'\s*[:：|,，]*\s*(?:(?:标准|限值|目标|范围|要求)\s*[:：]?\s*)?(?:[<>≤≥=＝]|不高于|不低于|小于|大于|至多|至少|±|RSD\s*[<>≤≥=])\s*[-+]?\d+(?:\.\d+)?'
        interval = r'\s*[:：|,，]*\s*[-+]?\d+(?:\.\d+)?\s*(?:~|～|—|至|-)\s*[-+]?\d+(?:\.\d+)?'
        if re.match(threshold, suffix, re.I) or re.match(interval, suffix):
            return 'process_has_controlled_metric', 'explicit_named_process_metric_threshold'
    return None


def _semantic_relation(source: dict, target: dict, text: str, vocabulary=None):
    """Match a predicate between THESE endpoints, never a nearby other pair.

    Fail closed on negation, sentence boundaries or an intervening process.
    Numeric metric values remain source quotes; they are never actual yields.
    """
    if vocabulary is not None:
        return _configured_semantic_relation(source, target, text, vocabulary)
    pair = (source['entity_type'], target['entity_type'])
    if pair != ('process', 'metric') and source['end'] > target['start']:
        return None
    left, right = min(source['start'], target['start']), max(source['end'], target['end'])
    window = _without_whitespace(matching_view(text[max(0, left - 180):min(len(text), right + 220)]))
    between = _without_whitespace(matching_view(text[min(source['end'], target['end']):max(source['start'], target['start'])]))
    if len(between) > 180 or re.search(r'[。；;]|禁止|无关|不得', between):
        return None
    suffix = _without_whitespace(matching_view(text[target['end']:target['end'] + 45]))
    if pair == ('product', 'material'):
        # Recipe tables may put the product heading many rows before the raw
        # material. The explicit 处方组成/原料名称 labels are the predicate.
        if re.search(r'(?:处方组成|原料名称|处方量|配方)', window):
            return 'product_contains_material', 'explicit_recipe_table_label'
    elif pair == ('material', 'process'):
        # A flow branch can contain whitespace, box-drawing glyphs and arrows,
        # but cannot jump over a different material or process node.
        if re.fullmatch(r'[─━┬┴┐┘┌└├┤│┼++>→↓]*(?:经|经过|进入|采用|进行)?', between) and between:
            return 'material_undergoes_process', 'direct_flow_arrow_or_predicate'
    elif pair == ('process', 'equipment'):
        # e.g. 灌装 (10ml/支, 灌封一体机), 总混 (三维混合机).
        if re.fullmatch(r'\([^()。；;]{0,40}', between) and ')' in suffix:
            return 'process_uses_equipment', 'equipment_inside_process_parenthesis'
        if re.fullmatch(r'(?:工序)?(?:使用|采用|进入|通过)(?:设备)?[:：]?', between):
            return 'process_uses_equipment', 'explicit_equipment_use_predicate'
    elif pair == ('process', 'metric'):
        # The source tables put the process in one cell and metric/value in a
        # neighbouring cell, often separated by line breaks after PDF parsing.
        if abs(source['start'] - target['start']) > 80:
            return None
        metric_name = _without_whitespace(matching_view(target['name']))
        process_name = _without_whitespace(matching_view(source['name']))
        metric_process_alias = {'装量合格率': {'分装'}, '密封合格率': {'包装'},
                                '干燥水分': {'干燥'}}
        if not (metric_name.startswith(process_name) or process_name in metric_process_alias.get(target['name'], set())):
            return None
        row_window = _without_whitespace(matching_view(text[max(0, left - 30):min(len(text), right + 70)]))
        if re.search(r'(?:收率|损耗|合格率|水分|均匀度|装量)', row_window) and re.search(
                r'(?:≥|≤|<>|＝|=|±|RSD|%)', row_window):
            return 'process_has_controlled_metric', 'controlled_parameter_table_row'
    return None


def _vocabulary(meta: dict):
    configured = vocabulary_from_metadata(meta)
    if configured is not None:
        return {kind: {name: tuple(values) for name, values in names.items()}
                for kind, names in configured['entities'].items()}
    # Explicit other-domain documents without a reviewed vocabulary stay empty,
    # not silently pharmaceutical. Scopes do not supply any aliases in V4.
    if explicit_other_domain(meta):
        return {kind: {} for kind in ENTITY_TYPES}
    aliases = {kind: {name: tuple(values) for name, values in values.items()}
               for kind, values in ENTITY_ALIASES.items()}
    # Explicitly scoped products are accepted as vocabulary only when their
    # canonical form appears in the source text.  This supports future products
    # without inferring an edge from authorization metadata alone.
    for product in meta.get('scope_products') or []:
        if isinstance(product, str) and product.strip() and product != '*':
            aliases['product'].setdefault(product, (product,))
    return aliases


def extract_entities(row: dict):
    text = row['text']
    aliases = _vocabulary(row.get('meta') or {})
    found = []
    for entity_type, names in aliases.items():
        candidates = []
        for canonical, values in names.items():
            for alias in values:
                for start, end in _alias_spans(text, alias):
                    candidates.append((start, -(end - start), canonical, alias, end))
        # Prefer the longest alias at an overlapping position.  Two different
        # canonical entities are retained only when their raw spans do not overlap.
        selected = []
        for start, neg_length, canonical, alias, end in sorted(candidates):
            if any(start < old_end and old_start < end for old_start, old_end, *_ in selected):
                continue
            selected.append((start, end, canonical, alias))
        for start, end, canonical, alias in selected:
            found.append({'entity_id': _stable_id(entity_type, canonical), 'entity_type': entity_type,
                          'name': canonical, 'alias': alias, 'start': start, 'end': end})
    return sorted(found, key=lambda item: (item['start'], item['end'], item['entity_type'], item['name']))


class DomainGraphIndex(defaultdict):
    """Authorized in-memory domain graph plus legacy term->chunk mapping.

    The defaultdict base is intentionally retained so old callers and old graph
    route tests can continue to inspect the term-evidence graph unchanged.
    """

    def __init__(self):
        super().__init__(set)
        self.entities = {}
        self.mentions = []
        self.relations = []
        self.chunk_entities = defaultdict(set)
        self.chunk_relations = defaultdict(list)
        self._aliases = defaultdict(set)
        self.diagnostics = []

    def add_entity(self, entity_type: str, name: str, aliases: Iterable[str] = ()) -> str:
        entity_id = _stable_id(entity_type, name)
        entry = self.entities.setdefault(entity_id, {'entity_id': entity_id, 'entity_type': entity_type,
                                                      'name': name, 'aliases': []})
        for alias in aliases:
            if alias and alias not in entry['aliases']:
                entry['aliases'].append(alias)
            self._aliases[_without_whitespace(matching_view(alias))].add(entity_id)
        self._aliases[_without_whitespace(matching_view(name))].add(entity_id)
        return entity_id

    def add_mention(self, mention: dict, source: dict):
        entity_id = mention['entity_id']
        self.entities.setdefault(entity_id, {'entity_id': entity_id, 'entity_type': mention['entity_type'],
                                              'name': mention['name'], 'aliases': [mention['alias']]})
        record = {'entity_id': entity_id, 'entity_type': mention['entity_type'], 'name': mention['name'],
                  'alias': mention['alias'], 'chunk_id': source['chunk_id'], 'version_id': source['version_id'],
                  'start': source['offset'], 'end': source['end_offset'], 'quote': source['quote'],
                  'source': {key: value for key, value in source.items() if key not in {'quote'}}}
        self.mentions.append(record)
        self.chunk_entities[source['chunk_id']].add(entity_id)
        self[_without_whitespace(matching_view(mention['name']))].add(source['chunk_id'])
        self[_without_whitespace(matching_view(mention['alias']))].add(source['chunk_id'])

    def add_relation(self, relation: dict):
        self.relations.append(relation)
        self.chunk_relations[relation['support']['chunk_id']].append(relation)

    def query_entities(self, query: str):
        view = _without_whitespace(matching_view(query))
        matches = {}
        for key, entity_ids in self._aliases.items():
            if key and key in view:
                for entity_id in entity_ids:
                    matches[entity_id] = {'entity_id': entity_id, 'matched_alias': key,
                                          'entity': self.entities.get(entity_id)}
        # When loaded from SQLite, _aliases can be reconstructed from entities;
        # this branch also makes manually assembled fixtures useful.
        if not matches:
            for entity_id, entity in self.entities.items():
                for alias in [entity.get('name', ''), *entity.get('aliases', [])]:
                    key = _without_whitespace(matching_view(alias))
                    if key and key in view:
                        matches[entity_id] = {'entity_id': entity_id, 'matched_alias': key, 'entity': entity}
        return list(matches.values())

    def domain_search(self, query: str):
        query_matches = self.query_entities(query)
        if not query_matches:
            return [], {'matched_entities': [], 'relation_hits': []}
        query_ids = {item['entity_id'] for item in query_matches}
        query_types = {item['entity'].get('entity_type') for item in query_matches if item.get('entity')}
        scores = defaultdict(float)
        relation_hits = defaultdict(list)
        for mention in self.mentions:
            if mention['entity_id'] in query_ids:
                # A direct entity hit is stronger than a relation-only hit.
                scores[mention['chunk_id']] += 3.0 if mention['alias'] == mention['name'] else 2.5
        for relation in self.relations:
            if relation.get('semantic_status') != 'semantic_template_supported':
                continue  # labelled co-occurrence is inspectable, not a semantic vote
            source_id, target_id = relation['source_entity_id'], relation['target_entity_id']
            relation_weight = 2.4
            if source_id in query_ids or target_id in query_ids:
                scores[relation['support']['chunk_id']] += relation_weight
                relation_hits[relation['support']['chunk_id']].append(relation['relation_id'])
            if source_id in query_ids and target_id in query_ids:
                scores[relation['support']['chunk_id']] += 3.0
            # Product-material and process-equipment pairs are useful bridge
            # evidence even when the query names only one side.
            relation_types = {self.entities.get(source_id, {}).get('entity_type'),
                              self.entities.get(target_id, {}).get('entity_type')}
            if relation_types & query_types and (source_id in query_ids or target_id in query_ids):
                scores[relation['support']['chunk_id']] += 0.5
        # Two semantic edges may bridge material -> process -> equipment.
        # Each hop must be supported by the same authorized release. Co-occurs
        # edges cannot propagate to another entity or chunk.
        first_hop = set()
        for relation in self.relations:
            if relation.get('semantic_status') != 'semantic_template_supported':
                continue
            endpoints = {relation['source_entity_id'], relation['target_entity_id']}
            if endpoints & query_ids:
                first_hop.update(endpoints - query_ids)
        for relation in self.relations:
            if relation.get('semantic_status') != 'semantic_template_supported':
                continue
            endpoints = {relation['source_entity_id'], relation['target_entity_id']}
            if endpoints & first_hop and not endpoints & query_ids:
                chunk = relation['support']['chunk_id']
                scores[chunk] += 0.6
                relation_hits[chunk].append(relation['relation_id'])
        ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        trace = {'matched_entities': query_matches, 'semantic_max_hops': 2,
                 'relation_hits': {chunk: ids for chunk, ids in relation_hits.items()}}
        return ordered, trace

    def evidence_for_chunk(self, chunk_id: str, query: str = ''):
        query_ids = {item['entity_id'] for item in self.query_entities(query)} if query else set()
        result = []
        for relation in self.chunk_relations.get(chunk_id, []):
            if relation.get('semantic_status') != 'semantic_template_supported':
                continue
            if query_ids and not ({relation['source_entity_id'], relation['target_entity_id']} & query_ids):
                continue
            result.append({key: relation[key] for key in ('relation_id', 'source_entity_id', 'target_entity_id',
                                                           'relation_type', 'semantic_status', 'support')})
        return result


def build_domain_graph(rows: Iterable[dict]) -> DomainGraphIndex:
    rows = list(rows)
    frozen_vocabulary = any(vocabulary_from_metadata(row.get('meta') or {}) is not None
                            or explicit_other_domain(row.get('meta') or {}) for row in rows)
    index = DomainGraphIndex()
    for row in rows:
        mentions = extract_entities(row)
        meta = row.get('meta') or {}
        aliases = _vocabulary(meta)
        vocabulary = vocabulary_from_metadata(meta)
        for mention in mentions:
            index.add_entity(mention['entity_type'], mention['name'], aliases.get(mention['entity_type'], {}).get(mention['name'], ()))
        for left in mentions:
            # Mentions in the same chunk can be related only when the exact
            # source spans are available; the quote always covers both mentions.
            left_source = _source_for_span(row, left['start'], left['end'])
            left_source['quote'] = row['text'][left['start']:left['end']]
            if frozen_vocabulary:
                # Freeze aliases per source, not only in the merged entity table:
                # another inaccessible document may give the same name aliases.
                left_source['vocabulary_aliases'] = list(aliases[left['entity_type']][left['name']])
            index.add_mention(left, left_source)
        by_type = defaultdict(list)
        for mention in mentions:
            by_type[mention['entity_type']].append(mention)

        row_sections = _flow_sections(row)

        def add_relation(source, target, relation_type, *, semantic=False, template=None, section=None):
            local_start, local_end = min(source['start'], target['start']), max(source['end'], target['end'])
            section = section or next((s for s in row_sections
                if s['offset'] <= local_start and local_end <= s['end_offset']), None)
            if semantic and template and relation_type != 'process_precedes_process':
                local_start, local_end = _matching_excerpt(row['text'], local_start, local_end)
                if section:
                    local_start = max(local_start, section['offset'])
                    local_end = min(local_end, section['end_offset'])
            support = _source_for_span(row, local_start, local_end)
            products = list(section['products']) if section and section['kind'] == 'product' else []
            applicability = {**(meta.get('applicability') or {}),
                             'kind': section['kind'] if section else 'unknown',
                             'products': products, 'section': section['section'] if section else '',
                             'basis': section['basis'] if section else 'no_single_section'}
            quote = row['text'][local_start:local_end]
            support.update({
                'quote': quote,
                'quote_sha256': hashlib.sha256(quote.encode('utf-8')).hexdigest(),
                'applicability': applicability,
                'section': applicability['section'],
                'evidence_scope': {
                    'products': products,
                    'factories': list(meta.get('scope_factories') or []),
                },
                'evidence_template': template,
            })
            if vocabulary is not None:
                support['claim_boundary'] = ('Source-supported reference relationship only; not an actual '
                    'operating event, material consumption, physical yield, cost effect or causal conclusion.')
            if relation_type == 'process_precedes_process':
                support['source_process_span'] = {
                    'offset': int(meta.get('offset', 0)) + source['start'],
                    'end_offset': int(meta.get('offset', 0)) + source['end'],
                    'quote': row['text'][source['start']:source['end']]}
                support['target_process_span'] = {
                    'offset': int(meta.get('offset', 0)) + target['start'],
                    'end_offset': int(meta.get('offset', 0)) + target['end'],
                    'quote': row['text'][target['start']:target['end']]}
                support['claim_boundary'] = ('Explicit text-link only; canonical process aliases are not '
                    'step occurrence IDs. Join route steps by version and exact occurrence spans, '
                    'not canonical entity IDs; unsupported layouts are not reconstructed.')
            relation = {
                'relation_id': _relation_id(source['entity_id'], target['entity_id'], relation_type,
                                            row['chunk_id'], support['offset'], support['end_offset']),
                'source_entity_id': source['entity_id'],
                'target_entity_id': target['entity_id'],
                'relation_type': relation_type,
                'source_type': source['entity_type'],
                'target_type': target['entity_type'],
                'semantic_status': 'semantic_template_supported' if semantic else 'co_occurrence_only',
                'support': support,
            }
            index.add_relation(relation)

        # First persist semantic edges only where an explicit reviewed template
        # is present.  Direction is canonical even when the source text reverses
        # the mention order.
        semantic_pairs = (
            ('product', 'material', 'product_contains_material'),
            ('material', 'process', 'material_undergoes_process'),
            ('process', 'equipment', 'process_uses_equipment'),
            ('process', 'metric', 'process_has_controlled_metric'),
        )
        semantic_keys = set()
        for source_type, target_type, relation_type in semantic_pairs:
            for source in by_type[source_type]:
                for target in by_type[target_type]:
                    if source['entity_id'] == target['entity_id']:
                        continue
                    if vocabulary is not None and not any(
                            section['offset'] <= min(source['start'], target['start'])
                            and max(source['end'], target['end']) <= section['end_offset']
                            for section in row_sections):
                        continue
                    detected = _semantic_relation(source, target, row['text'], vocabulary)
                    if detected and detected[0] == relation_type:
                        add_relation(source, target, relation_type, semantic=True, template=detected[1])
                        semantic_keys.add((source['entity_id'], target['entity_id']))

        flows, diagnostics = _text_flow_relations(row, aliases)
        for source, target, section, template in flows:
            add_relation(source, target, 'process_precedes_process', semantic=True,
                         template=template, section=section)
        for diagnostic in diagnostics:
            support = _source_for_span(row, diagnostic['start'], diagnostic['end'])
            support['quote'] = row['text'][diagnostic['start']:diagnostic['end']]
            index.diagnostics.append({**diagnostic, 'source': support})

        # Keep other same-chunk pairs for retrieval connectivity, explicitly
        # labelled as co-occurrence so no caller can mistake them for a business
        # predicate or causal edge.
        for source_type, target_type in COOCCURRENCE_RULES:
            for source in by_type[source_type]:
                for target in by_type[target_type]:
                    if source['entity_id'] == target['entity_id']:
                        continue
                    if (source['entity_id'], target['entity_id']) in semantic_keys:
                        continue
                    add_relation(source, target, 'entity_co_occurs_in_chunk', semantic=False,
                                 template='same_confirmed_chunk')
    # Recipe tables commonly start in a later section chunk than the product
    # title. Link only inside the same confirmed version and retain BOTH raw
    # supports. Authorization metadata alone cannot create this relation.
    product_mentions = defaultdict(list)
    for mention in index.mentions:
        if mention['entity_type'] == 'product':
            product_mentions[mention['version_id']].append(mention)
    for row in rows:
        meta = row['meta']
        vocabulary = vocabulary_from_metadata(meta)
        title_pattern = r'配方|BOM|物料清单|材料清单' if vocabulary is not None else r'配方'
        if not re.search(title_pattern, matching_view(meta.get('title', '')), re.I):
            continue
        table_view = matching_view(row['text'])
        label_pattern = (r'BOM|物料清单|材料清单|材料名称|物料名称|处方组成|原料名称|原材料|处方量'
                         if vocabulary is not None else r'处方组成|原料名称|原材料|处方量')
        if not re.search(label_pattern, table_view, re.I):
            continue
        products = product_mentions.get(row['version_id'], [])
        product_ids = {item['entity_id'] for item in products}
        if len(product_ids) != 1:
            continue
        product = products[0]
        for material in extract_entities(row):
            if material['entity_type'] != 'material':
                continue
            # A recipe row must carry an explicit quantity or amount marker;
            # an unrelated mention in prose cannot become an ingredient row.
            raw_suffix = row['text'][material['end']:material['end'] + 40]
            quantity = (r'\s*[|,，\t]*\s*(?:\d+(?:\.\d+)?|加[至⾄])' if vocabulary is not None
                        else r'\s*(?:[\d.,]+\s*(?:kg|g|L|毫克|克)?|加[至⾄])')
            if not re.match(quantity, matching_view(raw_suffix)):
                continue
            if vocabulary is not None:
                section = next((s for s in _flow_sections(row)
                                if s['offset'] <= material['start'] < s['end_offset']), None)
                if (not section or section['kind'] != 'product'
                        or section['products'] != [product['name']]):
                    continue
            line_start = row['text'].rfind('\n', 0, material['start']) + 1
            line_end = row['text'].find('\n', material['end'])
            if line_end < 0:
                line_end = len(row['text'])
            support = _source_for_span(row, line_start, line_end)
            if vocabulary is not None:
                raw_line = row['text'][line_start:line_end]
                prefix = _without_whitespace(matching_view(row['text'][line_start:material['start']]))
                if prefix.strip('|,，') or re.search(r'不含|不得|不使用|禁止|取消', matching_view(raw_line)):
                    continue
                # Keep an inspectable raw table-header predicate, not merely
                # an opaque template name or a document title from metadata.
                header = next((match for match in re.finditer(label_pattern, row['text'], re.I)
                               if match.start() < material['start']), None)
                if header is None or header.start() < section['offset']:
                    continue
                header_start = row['text'].rfind('\n', 0, header.start()) + 1
                header_end = row['text'].find('\n', header.end())
                if header_end < 0:
                    header_end = len(row['text'])
                header_source = _source_for_span(row, header_start, header_end)
                header_source['quote'] = row['text'][header_start:header_end]
                header_source['quote_sha256'] = hashlib.sha256(header_source['quote'].encode('utf-8')).hexdigest()
                product_row = next((item for item in rows if item['chunk_id'] == product['chunk_id']), None)
                # Mention source offsets are already absolute; recover the
                # containing physical heading from the original chunk offset.
                local = product['start'] - int(product_row['meta'].get('offset', 0)) if product_row else -1
                if local < 0:
                    continue
                heading_start = product_row['text'].rfind('\n', 0, local) + 1
                heading_end = product_row['text'].find('\n', local + len(product['quote']))
                if heading_end < 0:
                    heading_end = len(product_row['text'])
                heading = product_row['text'][heading_start:heading_end]
                heading_view = _without_whitespace(matching_view(heading))
                product_view = re.escape(_without_whitespace(matching_view(product['quote'])))
                if not re.fullmatch(r'(?:产品(?:名称)?[:：])?' + product_view
                                    + r'(?:BOM|物料清单|材料清单|配方|生产工艺)?', heading_view, re.I):
                    continue
                product_source = _source_for_span(product_row, heading_start, heading_end)
                product_source.update(quote=heading, quote_sha256=hashlib.sha256(heading.encode('utf-8')).hexdigest())
            support.update(quote=row['text'][line_start:line_end],
                           quote_sha256=hashlib.sha256(row['text'][line_start:line_end].encode('utf-8')).hexdigest(),
                           evidence_template='recipe_table_with_same_version_product_title',
                           evidence_scope={'products': list((meta.get('applicability') or {}).get('products') or []),
                                           'factories': list(meta.get('scope_factories') or [])},
                           context_sources=[{**product['source'], 'quote': product['quote']}],
                           applicability=meta.get('applicability', {}),
                           claim_boundary='配方参考原文，不是实际投料、采购价或单耗')
            if vocabulary is not None:
                support.update(evidence_template='explicit_bom_row_with_source_product_heading',
                    context_sources=[product_source, header_source],
                    applicability={**(meta.get('applicability') or {}), 'kind': section['kind'],
                                   'products': list(section['products']), 'section': section['section'],
                                   'basis': section['basis']},
                    evidence_scope={'products': list(section['products']),
                                    'factories': list(meta.get('scope_factories') or [])},
                    claim_boundary='BOM/reference source only; not actual material consumption, yield or causality')
            index.add_relation({
                'relation_id': _relation_id(product['entity_id'], material['entity_id'], 'product_contains_material',
                                           row['chunk_id'], support['offset'], support['end_offset']),
                'source_entity_id': product['entity_id'], 'target_entity_id': material['entity_id'],
                'relation_type': 'product_contains_material', 'source_type': 'product', 'target_type': 'material',
                'semantic_status': 'semantic_template_supported', 'support': support,
            })
    # Deduplicate repeated aliases/relations while preserving deterministic order.
    index.mentions = sorted({(m['entity_id'], m['chunk_id'], m['start'], m['end']): m for m in index.mentions}.values(),
                             key=lambda item: (item['chunk_id'], item['start'], item['entity_id']))
    unique_relations = {relation['relation_id']: relation for relation in index.relations}
    index.relations = [unique_relations[key] for key in sorted(unique_relations)]
    index.chunk_relations = defaultdict(list)
    for relation in index.relations:
        index.chunk_relations[relation['support']['chunk_id']].append(relation)
    for entity in index.entities.values():
        entity['aliases'] = sorted(set(entity['aliases']))
    return index


def load_domain_graph(conn, permitted_version_ids: Iterable[str], *, frozen_vocabulary=None):
    """Load only domain rows whose supporting chunks are authorized."""
    if frozen_vocabulary is None:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_configuration'").fetchone()
        frozen_vocabulary = bool(exists and conn.execute('SELECT 1 FROM graph_configuration WHERE singleton=1').fetchone())
    index = DomainGraphIndex()
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS domain_permitted(version_id TEXT PRIMARY KEY)')
    conn.execute('DELETE FROM domain_permitted')
    conn.executemany('INSERT INTO domain_permitted VALUES(?)', [(value,) for value in sorted(set(permitted_version_ids))])
    try:
        mentions = conn.execute('''SELECT m.entity_id,m.entity_type,m.name,m.alias,m.chunk_id,m.version_id,
                                          m.start,m.end,m.quote,m.source
                                     FROM domain_mentions m JOIN domain_permitted p ON p.version_id=m.version_id''').fetchall()
    except Exception:
        return index
    entity_ids = {row[0] for row in mentions}
    if entity_ids and not frozen_vocabulary:
        placeholders = ','.join('?' for _ in entity_ids)
        for row in conn.execute(f'SELECT entity_id,entity_type,name,aliases FROM domain_entities WHERE entity_id IN ({placeholders})', tuple(sorted(entity_ids))):
            index.add_entity(row[1], row[2], json.loads(row[3]))
    for row in mentions:
        source = json.loads(row[9])
        source.update(chunk_id=row[4], version_id=row[5], offset=row[6], end_offset=row[7], quote=row[8])
        if frozen_vocabulary:
            index.add_entity(row[1], row[2], source.get('vocabulary_aliases', [row[2], row[3]]))
        index.add_mention({'entity_id': row[0], 'entity_type': row[1], 'name': row[2], 'alias': row[3],
                           'start': row[6], 'end': row[7]}, source)
    try:
        relations = conn.execute('''SELECT r.relation_id,r.source_entity_id,r.target_entity_id,r.relation_type,
                                           r.source_type,r.target_type,r.semantic_status,r.support
                                      FROM domain_relations r JOIN domain_permitted p ON p.version_id=r.version_id''').fetchall()
    except Exception:
        relations = []
    for row in relations:
        index.add_relation({'relation_id': row[0], 'source_entity_id': row[1], 'target_entity_id': row[2],
                            'relation_type': row[3], 'source_type': row[4], 'target_type': row[5],
                            'semantic_status': row[6], 'support': json.loads(row[7])})
    return index


def domain_graph_counts(index: DomainGraphIndex):
    return {'entities': len(index.entities), 'mentions': len(index.mentions), 'relations': len(index.relations),
            'chunks_with_entities': len(index.chunk_entities),
            'fingerprint': hashlib.sha256(json.dumps(
                {'entities': sorted(index.entities.values(), key=lambda item: item['entity_id']),
                 'relations': index.relations}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()}

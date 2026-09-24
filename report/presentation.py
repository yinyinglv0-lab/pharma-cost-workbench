"""Readable citations and a separate audit sidecar, without mutating frozen sources."""
from copy import deepcopy
from pathlib import PureWindowsPath
import re

VERSION = 'reading-citations/1.0'


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in ('text', 'rows', 'headers', 'note', 'caption'):
                yield from _strings(item)


def _leaves(source):
    if source.get('records'):
        return [leaf for record in source['records'] if isinstance(record, dict) for leaf in _leaves(record)]
    return [source]


def _ranges(values):
    ordered = sorted(set(values))
    result, start, end = [], None, None
    for value in ordered:
        if start is None:
            start = end = value
        elif value == end + 1:
            end = value
        else:
            result.append(str(start) if start == end else f'{start}–{end}')
            start = end = value
    if start is not None:
        result.append(str(start) if start == end else f'{start}–{end}')
    return '、'.join(result)


def _record_location(source):
    groups = {}
    for leaf in _leaves(source):
        filename = PureWindowsPath(str(leaf.get('file') or leaf.get('table') or '受控来源')).name
        key = (filename, leaf.get('sheet'))
        group = groups.setdefault(key, {'records': [], 'lines': [], 'months': []})
        for field, target in (('record_number', 'records'), ('line', 'lines')):
            if type(leaf.get(field)) is int:
                group[target].append(leaf[field])
        months = leaf.get('key', {}).get('月份', [])
        group['months'].extend(months if isinstance(months, list) else [str(months)])
    locations = []
    for (filename, sheet), values in groups.items():
        location = filename + ('／' + str(sheet) if sheet else '')
        if values['records']:
            location += '；记录' + _ranges(values['records'])
        elif values['lines']:
            location += '；行' + _ranges(values['lines'])
        elif values['months']:
            location += '；' + '、'.join(sorted(set(values['months'])))
        locations.append(location)
    return '\n'.join(locations)


def reading_appendix(payload, blocks):
    """Map only visible citations to short IDs; quote spans come from claim cards.

    No chunk-prefix truncation: a selected labor quote near a chunk's end stays
    intact. All original evidence IDs, raw quotes, offsets and hashes stay in the
    sealed reading_citations map and sources, and are downloadable in the sidecar.
    """
    sources = {row['id']: row for row in payload['sources']}
    used = []
    for block in blocks:
        for value in _strings(block):
            for match in re.finditer(r'\[([^\[\]\n]+)\]', value):
                ident = match[1]
                if ident in sources and ident not in used:
                    used.append(ident)
    mapping = {ident: str(index) for index, ident in enumerate(used, 1)}
    cards = {}
    for claim in [*payload.get('knowledge_usage', {}).get('claim_ledger', []),
                  *payload.get('benchmark', {}).get('analysis', {}).get('claim_ledger', [])]:
        for card in claim.get('quote_cards', []):
            signature = (card['evidence_id'], card['quote_start'], card['quote_end'])
            cards.setdefault(signature, deepcopy(card))
    catalog, rows = [], []
    for ident in used:
        source = sources[ident]
        quotes = [card for (ref, _, _), card in cards.items() if ref == ident]
        if source.get('kind') == 'document_basis':
            if not quotes:
                raise ValueError('正文知识引用缺少精确引文：' + ident)
            summary = '\n\n'.join(card['quote'] for card in quotes)
            location = '\n'.join(dict.fromkeys(PureWindowsPath(card['source'].get('file', '受控文档')).name + '；' + card['location'] for card in quotes))
        else:
            summary, location = source['text'], _record_location(source['source'])
        rows.append(['[' + mapping[ident] + ']', summary, location])
        catalog.append({'display_id': mapping[ident], 'evidence_id': ident,
                        'kind': source.get('kind'), 'quote_cards': quotes,
                        'source': deepcopy(source.get('source')), 'document_sha256': source.get('document_sha256')})

    def substitute(value):
        if isinstance(value, str):
            return re.sub(r'\[([^\[\]\n]+)\]', lambda m: '[' + mapping[m[1]] + ']' if m[1] in mapping else m[0], value)
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        return value

    result = substitute(deepcopy(blocks))
    weights = {'metrics': [19, 13, 13, 10, 13, 10, 12, 10], 'structure': [20, 12, 20, 12, 21, 15],
               'material': [29, 13, 13, 20, 19, 6], 'mfg': [26, 14, 14, 20, 20, 6],
               'labor': [40, 20, 20, 20], 'labor_bridge': [28, 36, 36],
               'trend': [16, 18, 13, 13, 13, 14, 13], 'market': [20, 17, 19, 19, 17, 8],
               'benchmark': [14, 15, 13, 13, 11, 13, 21], 'benchmark_structure': [17, 24, 17, 21, 21]}
    for block in result:
        if block.get('kind') == 'table' and block.get('name') in weights:
            block['column_weights'] = weights[block['name']]
    result.extend([{'kind': 'heading', 'level': 1, 'text': '附录一：正文引用与原文摘录'},
                   {'kind': 'paragraph', 'text': '编号按正文首次引用顺序排列；记录序号包含表头。工艺原文用于说明标准和核对方向，实际执行情况仍以当期业务凭证为准。'},
                   {'kind': 'table', 'name': 'citations', 'headers': ['引用', '采用的事实或原文', '文件与定位'],
                    'rows': rows, 'column_weights': [8, 58, 34], 'numeric_columns': []},
                   {'kind': 'heading', 'level': 1, 'text': '附录二：报告存档说明'},
                   {'kind': 'paragraph', 'text': '本报告保存生成时的输入、引用和展示内容。独立审计附件提供完整来源、精确引文位置、版本、生成记录及校验摘要；历史报告和已归档文件保持原样。'}])
    return result, {'version': VERSION, 'entries': catalog,
                    'task_ids': [{'display_id': s['id'], 'task_id': t['id']} for s, t in zip(payload['suggestions'], payload['task_drafts'])]}


def audit_metadata(payload):
    """Verified, JSON-safe companion suitable for an authorized download endpoint."""
    from .model import verify_payload, digest
    verify_payload(payload)
    value = {'schema_version': 'report-audit/1.0', 'report_id': payload['report_id'],
             'render_payload_hash': payload['frozen_hash'],
             'source_payload_hash': payload.get('approval', {}).get('source_payload_hash', payload['frozen_hash']),
             'review_status': payload['review_status'], 'approval': deepcopy(payload.get('approval')),
             'analysis_run_id': payload['analysis_run_id'], 'created_at': payload['created_at'],
             'sources': deepcopy(payload['sources']), 'reading_citations': deepcopy(payload.get('reading_citations')),
             'versions': deepcopy(payload['versions']), 'generation': deepcopy(payload.get('generation')),
             'generation_status': payload.get('generation_status'), 'fallback_reason': payload.get('fallback_reason'),
             'validation': deepcopy(payload['validation']), 'warnings': deepcopy(payload['warnings']),
             'knowledge_usage': deepcopy(payload.get('knowledge_usage')),
             'cross_factory_knowledge_usage': deepcopy(payload.get('benchmark', {}).get('analysis', {}).get('knowledge_usage')),
             'task_drafts': deepcopy(payload['task_drafts']), 'blocks_sha256': digest(payload['blocks'])}
    value['audit_hash'] = digest(value)
    return value

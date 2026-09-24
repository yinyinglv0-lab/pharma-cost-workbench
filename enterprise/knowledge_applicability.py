"""Conservative product applicability over the immutable, parsed source text.

NFKC is a matching view only. Every range is a Python character offset into the
confirmed original text (end exclusive); no normalized offset is ever persisted.
Document authorization scopes are deliberately separate from content applicability.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata

MATCHING_VIEW = 'unicode-nfkc-cjk-radicals/1'
# CJK Radicals Supplement is not compatibility-decomposed by NFKC (unlike
# Kangxi radicals). PDF extraction in the source uses e.g. U+2EE9 for 黄.
# These orthographic equivalents are for matching only, never for source quotes.
_RADICALS = str.maketrans({'⻩': '黄', '⻛': '风', '⻋': '车'})
APPLICABILITY = {'name': 'product-section-applicability', 'version': 2,
                 'matching_view': MATCHING_VIEW, 'offset_unit': 'unicode_character',
                 'offset_end': 'exclusive'}
ELEMENT_WORDS = {
    '材料': ('材料', '原料', '原材料', '配方', '药材', '采购', '投料', '收率', '提取', '包材', '物料', '损耗', '填充'),
    '人工': ('人工', '工时', '人员', '考勤', '工资', '定员', '返工', '培训', '排班'),
    '制费': ('设备', '折旧', '能源', '能耗', '蒸汽', '电力', '维修', '维护', '制造费用', '分摊'),
}
_PAGE = re.compile(r'\[第(\d+)页\]')
# A top-level section always ends the preceding product's scope, even if its
# title is not recognized. Decimal subsections (2.1 etc.) inherit their chapter.
_CHAPTER = re.compile(r'^(?:[一二三四五六七八九十百]+[、.．]|第[一二三四五六七八九十百\d]+[章节]|\d+[、.．](?!\d)|#{1,2}\s+)')
_GENERAL = re.compile(r'通用(?:规范|要求|条款|规定|规则|工艺|标准)|共同(?:要求|规定)|全(?:部|部各类|厂)产品|所有产品|各产品均|各类药品|药品生产质量管理规范')
_SHARED_TABLE = re.compile(r'公用工程|跨产品|跨车间|车间对比|设备利用率')
_PRODUCT_HEADING = re.compile(r'^([^\s，,。；;:：]{2,40}?)\s*(?:生产工艺(?:路线|规程)?|工艺路线|工艺规程|操作规程|生产流程)')


def matching_view(text):
    return unicodedata.normalize('NFKC', text).translate(_RADICALS)


def element_terms(text):
    view = matching_view(text)
    return {element: [word for word in words if word in view]
            for element, words in ELEMENT_WORDS.items() if any(word in view for word in words)}


def _products(meta):
    return list(dict.fromkeys(p for p in (meta.get('scope_products') or [])
                             if isinstance(p, str) and p and p != '*'))


def _mentions(text, products):
    view = re.sub(r'\s+', '', matching_view(text))
    return [p for p in products if re.sub(r'\s+', '', matching_view(p)) in view]


def _base(meta):
    products = _products(meta)
    reviewed = (meta.get('business_metadata') or {}).get('applicability', {})
    # Public visibility is permission to read, not proof of universal relevance.
    if isinstance(reviewed, dict) and reviewed.get('kind') == 'general':
        return 'general', products or ['*'], 'reviewed_general'
    if _GENERAL.search(matching_view(str(meta.get('title') or ''))):
        return 'general', products or ['*'], 'general_document_title'
    if len(products) == 1:
        return 'product', products, 'document_scope'
    return 'unknown', [], 'no_product_section'


def _heading(line, products):
    view = matching_view(line).strip()
    if not view or len(view) > 140 or _PAGE.fullmatch(view):
        return False
    names = _mentions(view, products)
    return bool(_CHAPTER.match(view) or (
        _PRODUCT_HEADING.match(view) and len(view) <= 90 and not re.search(r'[。；;]', view)) or (
        _GENERAL.search(view) and len(view) <= 90 and not re.search(r'[。；;]', view)) or (
        names and len(view) <= 90 and not re.search(r'[。；;]', view)
        and (view in [matching_view(p) for p in products]
             or re.search(r'(?:生产工艺|工艺路线|工艺规程|操作规程|生产流程)', view))))


def source_sections(text, meta):
    """Partition at source headings, never at a product name inside prose.

    Unrecognized chapters of a multi-product document are unknown, not inherited
    from the last product. Explicit general provisions get a separate section.
    """
    products = _products(meta)
    default = _base(meta)
    sections, current, offset = [], (0, '', *default), 0
    for raw_line in text.splitlines(keepends=True):
        if _heading(raw_line, products):
            if offset > current[0]:
                sections.append(_section(current, offset))
            title = raw_line.strip()
            names = _mentions(title, products)
            view = matching_view(title)
            if len(names) > 1:
                state = ('cross_product', names, 'multiple_product_heading')
            elif names:
                state = ('product', names, 'product_heading')
            elif _SHARED_TABLE.search(view) and len(products) > 1:
                # 共用对比章节（如公用工程消耗/设备利用率）是工厂级机制内容，不属单一产品专属信息；
                # 作为 general 条款参与机制证据（claim_boundary 仍声明不证明本期发生）
                state = ('general', ['*'], 'shared_comparison_section')
            elif _GENERAL.search(view):
                state = ('general', products or ['*'], 'general_heading')
            elif _PRODUCT_HEADING.match(_CHAPTER.sub('', view).strip()):
                state = ('unknown', [], 'undeclared_product_heading')
            else:
                state = default
            current = (offset, title, *state)
        offset += len(raw_line)
    if offset > current[0]:
        sections.append(_section(current, offset))
    # Even a reviewed general heading must not conceal product-specific prose.
    # 共用对比章节天然包含各产品名（对比表），不适用此翻转。
    for section in sections:
        names = _mentions(text[section['offset']:section['end_offset']], products)
        if section['kind'] == 'general' and names and section['basis'] != 'shared_comparison_section':
            section.update(kind='cross_product', products=names, basis='general_section_contains_product_text')
        elif section['kind'] == 'product' and set(names) - set(section['products']):
            section.update(kind='cross_product', products=sorted(set(names) | set(section['products'])),
                           basis='section_contains_other_product')
    return sections


def _section(current, end):
    start, title, kind, products, basis = current
    return {'offset': start, 'end_offset': end, 'section': title, 'kind': kind,
            'products': list(products), 'basis': basis}


def page_spans(text, start, end):
    pages = list(_PAGE.finditer(text))
    result = []
    for index, marker in enumerate(pages):
        stop = pages[index + 1].start() if index + 1 < len(pages) else len(text)
        left, right = max(start, marker.start()), min(end, stop)
        if left < right:
            result.append({'page': int(marker.group(1)), 'offset': left, 'end_offset': right})
    return result


def range_applicability(text, meta, start, end, *, sections=None, legacy=False):
    """Describe a whole chunk; legacy chunks crossing sections are never split."""
    sections = source_sections(text, meta) if sections is None else sections
    hits = [s for s in sections if s['offset'] < end and start < s['end_offset']
            and text[max(start, s['offset']):min(end, s['end_offset'])].strip()]
    products = sorted({p for s in hits for p in s['products']})
    kinds = {s['kind'] for s in hits}
    ranges = {(s['kind'], tuple(s['products'])) for s in hits}
    if len(ranges) == 1:
        kind = hits[0]['kind']
        basis = hits[0]['basis']
    elif 'cross_product' in kinds or len(products) > 1 or 'product' in kinds:
        kind, basis = 'cross_product', 'cross_section_window'
    else:
        kind, basis = 'unknown', 'mixed_or_missing_scope'
    specs = (meta.get('business_metadata') or {}).get('scope_specifications') \
        or (meta.get('business_metadata') or {}).get('specification') or ['*']
    if isinstance(specs, str):
        specs = [part.strip() for part in specs.replace('，', ',').split(',') if part.strip()] or ['*']
    if not isinstance(specs, list) or not all(isinstance(s, str) and s for s in specs):
        specs = []
    eligible = kind in {'product', 'general'}
    if eligible:
        # 视觉增强解析内容须经人工核对（登记时显式选择机制依据并留痕）才可作为机制证据
        parse_meta = meta.get('parse_metadata') or {}
        governance = meta.get('business_metadata') or {}
        if parse_meta.get('vision_enhanced') is True and governance.get('vision_reviewed') is not True:
            eligible, basis = False, 'vision_enhanced_unreviewed'
    return {'schema_version': APPLICABILITY['version'], 'kind': kind, 'products': products,
            'specifications': list(specs), 'basis': basis,
            'sections': [{key: s[key] for key in ('section', 'offset', 'end_offset')} for s in hits],
            'section': ' / '.join(dict.fromkeys(s['section'] for s in hits if s['section'])),
            'legacy_release': legacy, 'eligible_for_mechanism': eligible,
            'offset': start, 'end_offset': end}


def chunk_metadata(text, meta, start, end, *, sections=None, legacy=False):
    result = deepcopy(meta)
    applicability = range_applicability(text, meta, start, end, sections=sections, legacy=legacy)
    spans = page_spans(text, start, end)
    result.update(offset=start, end_offset=end, applicability=applicability,
                  section=applicability['section'], pages=list(dict.fromkeys(s['page'] for s in spans)),
                  page_spans=spans, elements=list(element_terms(text[start:end])))
    if spans:
        result['page_hint'] = spans[0]['page']
    return result


def applicability_reason(applicability, product, specification=None):
    kind = applicability.get('kind')
    if kind not in {'product', 'general'} or not applicability.get('eligible_for_mechanism'):
        if applicability.get('basis') == 'vision_enhanced_unreviewed':
            return 'vision_enhanced_unreviewed'
        return 'cross_product_chunk' if kind == 'cross_product' else 'unknown_product_scope'
    products = applicability.get('products', [])
    if product not in products and not (kind == 'general' and products == ['*']):
        return 'product_not_applicable'
    specs = applicability.get('specifications', [])
    if specification is not None and specification not in specs and specs != ['*']:
        return 'specification_not_applicable'
    return None


def reference_applicability_reason(meta, product, specification=None, as_of=None, objects=None, *, profile=None):
    from enterprise.tabular_knowledge import reference_applicability_reason as reference_reason
    return reference_reason(meta, product, specification, as_of=as_of, objects=objects, profile=profile)


def evidence_policy(meta, product, specification=None, *, as_of=None, objects=None):
    """Separate permission to read from applicability and authority to explain.

    Older releases without v2 section metadata remain visible only as explicitly
    unverified background. No caller may upgrade those rows to mechanism evidence.
    Explicitly inapplicable v2 sections never enter the scoped result list.
    """
    applicability = deepcopy(meta.get('applicability') or {})
    governance = meta.get('business_metadata') or {}
    declared_role = governance.get('evidence_role', 'context_only')
    if meta.get('table_row'):
        reason = reference_applicability_reason(meta, product, specification,
            as_of=as_of or meta.get('query_as_of'), objects=objects)
        return {'included': reason is None, 'applicability': applicability,
                'applicability_status': reason or 'applicable_reference',
                'evidence_role': declared_role if reason is None else 'context_only',
                'declared_evidence_role': declared_role,
                'claim_boundary': '仅为相应产品与期间的外部参考或观察基线；不证明本期实际采购、工艺事件或因果，不得充作机制依据。'}
    versioned = isinstance(applicability, dict) and applicability.get('schema_version') == APPLICABILITY['version']
    reason = applicability_reason(applicability, product, specification) if versioned else 'missing_applicability_v2'
    role = declared_role if reason is None else 'context_only'
    if role not in {'document_basis', 'context_only'}:
        role = 'context_only'
    return {'included': not versioned or reason is None,
            'applicability': applicability, 'applicability_status': reason or 'applicable',
            'evidence_role': role, 'declared_evidence_role': declared_role,
            'claim_boundary': ('适用的文档机制依据；不能证明本期实际发生。' if role == 'document_basis'
                               else '仅供背景阅读；未验证产品适用性或未获机制依据授权，不能支持经营原因。')}

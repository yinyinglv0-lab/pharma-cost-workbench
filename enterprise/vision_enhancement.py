# -*- coding: utf-8 -*-
"""PDF 视觉增强解析：对无文本页/图片页调用视觉模型补全文本（仅候选解析稿）。

- 页面图像来源：优先 PyMuPDF(fitz) 整页渲染（覆盖扫描页/表格页/图文混排），
  无 fitz 时退回 pypdf 提取页内嵌图（仅覆盖扫描件/整页图片）；
- 触发：仅处理预览覆盖信息标记的候选页（pages_without_text / image_pages），
  每次调用由页面按钮显式触发，逐页付费、失败不重试；
- 纪律：输出仅作候选解析稿，merged_text 须经 stage(extracted_text_override=...)
  走"生成差异预览 → 人工确认"管线；原件（原 PDF）始终作为 blob 保留。
"""
from __future__ import annotations

import io

MAX_PAGES = 10
VISION_HEADER = '【视觉增强解析稿】'


def candidate_pages(parsed) -> list:
    """返回需要视觉增强的页码（基于预览覆盖信息），最多 MAX_PAGES 页。"""
    parsed = parsed if isinstance(parsed, dict) else {}
    metadata = parsed.get('metadata') if isinstance(parsed.get('metadata'), dict) else {}
    coverage = metadata.get('coverage') if isinstance(metadata.get('coverage'), dict) else {}
    pages = set()
    for key in ('pages_without_text', 'image_pages'):
        for value in coverage.get(key) or []:
            if isinstance(value, int) and 1 <= value <= 10000:
                pages.add(value)
    return sorted(pages)[:MAX_PAGES]


def _page_images_fitz(content, numbers):
    """fitz 整页渲染候选页为 PNG（2x 缩放）。失败抛异常由调用方兜底。"""
    import fitz
    document = fitz.open(stream=content, filetype='pdf')
    images = []
    try:
        for number in numbers:
            page = document[number - 1]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            images.append((number, 'image/png', pixmap.tobytes('png')))
    finally:
        document.close()
    return images


def _page_images_pypdf(content, numbers):
    """pypdf 提取候选页内嵌图；返回按页聚合的 (页码, mime, bytes)。"""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(content))
    images = []
    for number in numbers:
        try:
            page = reader.pages[number - 1]
        except IndexError:
            continue
        for image in getattr(page, 'images', []) or []:
            if image.data and len(image.data) <= 10 * 1024 * 1024:
                mime = f'image/{image.name}' if image.name in ('png', 'jpeg', 'webp') else 'image/png'
                images.append((number, mime, image.data))
    return images


def render_pages(content, numbers):
    """返回 {页码: (mime, bytes)}：fitz 整页渲染优先，失败退回 pypdf 内嵌图。"""
    if not isinstance(content, bytes) or not content or not numbers:
        return {}
    try:
        result = {number: (mime, data) for number, mime, data in _page_images_fitz(content, numbers)}
    except Exception:
        result = {}
    if not result:
        result = {number: (mime, data) for number, mime, data in _page_images_pypdf(content, numbers)}
    return result


def merged_text(parsed, sections):
    """组装候选解析稿：免责声明 + 原提取正文 + 各页视觉结果（带页码标记）。"""
    lines = [f'{VISION_HEADER}（以下含视觉模型输出，未经人工核对不得作为正式依据）']
    original = str((parsed or {}).get('text') or '').strip()
    if original:
        lines.append(original)
    for section in sections:
        lines.append(f'[视觉增强·第{section["page"]}页]\n{str(section["text"]).strip()}')
    return '\n\n'.join(lines)


def enhance_pdf(content, parsed, *, task='扫描件页面', max_pages=MAX_PAGES, timeout=120.0):
    """视觉增强主入口。返回 {ok, sections, merged_text, meta, reason}；绝不抛异常。"""
    from enterprise.multimodal import TASKS, analyze_image, provider_config
    base = {'ok': False, 'sections': [], 'merged_text': '', 'meta': {},
            'reason': '', 'pages_missing': []}
    if task not in TASKS:
        base['reason'] = f'不支持的任务类型: {task}'
        return base
    if not isinstance(content, bytes) or not content:
        base['reason'] = 'PDF 内容为空'
        return base
    numbers = candidate_pages(parsed)[:max_pages]
    if not numbers:
        base['reason'] = '没有检测到需要视觉增强的页面（无文本页或图片页）；可人工核对原件后直接登记'
        return base
    try:
        provider, model, api_key, _ = provider_config()
    except ValueError as exc:
        base['reason'] = str(exc)
        return base
    if not api_key:
        base['reason'] = '视觉模型密钥未配置，请先在「模型配置」页保存供应商密钥'
        return base
    images = render_pages(content, numbers)
    base['pages_missing'] = [number for number in numbers if number not in images]
    if not images:
        base['reason'] = '无法从 PDF 取得候选页图像（无渲染器且无内嵌图），请人工核对原件'
        return base
    sections = []
    for number in sorted(images):
        mime, data = images[number]
        result = analyze_image(data, mime, task, timeout=timeout)
        if not result['ok']:
            base['ok'] = False
            base['sections'] = sections
            base['merged_text'] = merged_text(parsed, sections)
            base['meta'] = {'provider': provider, 'model': model, 'candidate_pages': numbers}
            base['reason'] = f'第{number}页视觉调用失败：{result.get("reason")}；已保留此前页结果，未重试'
            return base
        sections.append({'page': number, 'task': task, 'text': result['text'],
                         'provider': provider, 'model': model})
    return {'ok': True, 'sections': sections, 'merged_text': merged_text(parsed, sections),
            'meta': {'provider': provider, 'model': model, 'candidate_pages': numbers,
                     'pages_missing': base['pages_missing'], 'task': task},
            'reason': '', 'pages_missing': base['pages_missing']}

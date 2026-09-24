"""Authenticated HTTP adapters for the isolated canonical manufacturing service.

No caller-supplied identity, server path, default-domain switch, approval or send
operation is accepted. The backend supplies its current managed root at request
time so tests and deployments cannot accidentally bind an obsolete root.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from enterprise.domain_profiles import parse_domain_profile
from enterprise.manufacturing_adapter import parse_adapter_config
from enterprise.manufacturing_repository import (
    MAX_BUNDLE_BYTES, MAX_FILE_BYTES, ManufacturingConflict, ManufacturingRepository,
)
from enterprise.manufacturing_service import ManufacturingService
from enterprise.security import require


class StrictBody(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class InstallProfile(StrictBody):
    # JSON must stay raw until duplicate-key/nonfinite-aware domain parsers run.
    profile_json: str = Field(max_length=1_000_000)
    adapter_json: str = Field(max_length=1_000_000)
    expected_version: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)


class ConfirmData(StrictBody):
    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1000)


class ConfirmKnowledge(StrictBody):
    stage_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


class AnalysisScope(StrictBody):
    product: str = Field(min_length=1, max_length=200)
    specification: str = Field(min_length=1, max_length=200)
    month: str = Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')


class Analyze(AnalysisScope):
    use_llm: bool = False


class DraftTask(StrictBody):
    kind: Literal['attribution', 'benchmark']
    element: Literal['材料', '人工', '制费']


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except ManufacturingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


async def _strict_form(request, allowed):
    """Do not silently accept duplicate or actor/path override form fields."""
    form = await request.form()
    keys = [key for key, _ in form.multi_items()]
    if set(keys) - set(allowed) or len(keys) != len(set(keys)):
        raise ValueError('表单包含不允许或重复的字段')


def create_manufacturing_router(root_provider):
    router = APIRouter(prefix='/api/manufacturing', tags=['manufacturing'])

    def repository(request):
        return ManufacturingRepository(root_provider(), request.state.principal)

    def service(request):
        return ManufacturingService(root_provider(), request.state.principal)

    @router.get('/profiles')
    def profiles(request: Request):
        return {'profiles': repository(request).profiles()}

    @router.post('/profiles')
    def install(request: Request, body: InstallProfile):
        require(request.state.principal, 'system.configure')
        profile = parse_domain_profile(body.profile_json)
        adapter = parse_adapter_config(body.adapter_json).to_dict()
        return _call(repository(request).install_profile, profile, adapter,
                     expected_version=body.expected_version, reason=body.reason)

    @router.get('/profiles/{profile_id}/current')
    def current(request: Request, profile_id: str):
        snapshot = _call(repository(request).current, profile_id)
        return {key: value for key, value in snapshot.items() if key != 'runtime'}

    @router.post('/profiles/{profile_id}/stage')
    async def stage(request: Request, profile_id: str,
                    actual: Annotated[UploadFile, File()], budget: Annotated[UploadFile, File()],
                    materials: Annotated[UploadFile, File()], labor: Annotated[UploadFile, File()],
                    overhead: Annotated[UploadFile, File()],
                    periods_json: Annotated[str, Form(max_length=2000)],
                    expected_revision: Annotated[str, Form(max_length=20)]):
        require(request.state.principal, 'data.stage')
        await _strict_form(request, {'actual', 'budget', 'materials', 'labor', 'overhead',
                                     'periods_json', 'expected_revision'})
        # Multipart has strings, not JSON types; accept only canonical integers,
        # never true/1.0/negative revisions that can bypass compare-and-swap.
        import re
        if not re.fullmatch(r'0|[1-9][0-9]{0,18}', expected_revision):
            raise ValueError('期望版本须为非负整数')
        try:
            periods = json.loads(periods_json)
        except (ValueError, RecursionError):
            raise ValueError('periods_json须为明确月份的JSON数组') from None
        if (not isinstance(periods, list) or not 1 <= len(periods) <= 120
                or any(type(period) is not str for period in periods)):
            raise ValueError('须提供一至120个明确月份')
        uploads = dict(actual=actual, budget=budget, materials=materials, labor=labor, overhead=overhead)
        files, size = {}, 0
        for family, upload in uploads.items():
            content = await upload.read(MAX_FILE_BYTES + 1)
            if not 0 < len(content) <= MAX_FILE_BYTES:
                raise ValueError('CSV文件为空或超过20MiB')
            size += len(content)
            if size > MAX_BUNDLE_BYTES:
                raise ValueError('导入总量超过60MiB')
            files[family] = (upload.filename or '', content)
        return _call(repository(request).stage, profile_id, files, periods=periods,
                     expected_revision=int(expected_revision))

    @router.post('/stages/{stage_id}/confirm')
    def confirm(request: Request, stage_id: str, body: ConfirmData):
        return _call(repository(request).confirm, stage_id,
                     expected_revision=body.expected_revision, reason=body.reason)

    @router.post('/profiles/{profile_id}/knowledge/stage')
    async def stage_knowledge(request: Request, profile_id: str,
                             file: Annotated[UploadFile, File()],
                             title: Annotated[str, Form(min_length=1, max_length=200)],
                             product_ids_json: Annotated[str, Form(max_length=2000)],
                             category: Annotated[Literal['process', 'formula', 'equipment', 'industry_benchmark', 'market_prices', 'other'], Form()],
                             effective_from: Annotated[str, Form(max_length=10)],
                             reason: Annotated[str, Form(min_length=1, max_length=1000)],
                             effective_to: Annotated[str | None, Form(max_length=10)] = None):
        require(request.state.principal, 'knowledge.stage')
        await _strict_form(request, {'file', 'title', 'product_ids_json', 'category',
                                     'effective_from', 'effective_to', 'reason'})
        try:
            product_ids = json.loads(product_ids_json)
        except (ValueError, RecursionError):
            raise ValueError('product_ids_json须为受控产品标识的JSON数组') from None
        if (not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 100
                or any(type(item) is not str for item in product_ids)
                or len(set(product_ids)) != len(product_ids)):
            raise ValueError('须选择不重复的受控产品标识')
        content = await file.read(MAX_FILE_BYTES + 1)
        if not 0 < len(content) <= MAX_FILE_BYTES:
            raise ValueError('知识文件为空或超过20MiB')
        return _call(service(request).stage_knowledge, profile_id, content=content,
                     filename=file.filename or '', title=title, product_ids=product_ids,
                     category=category, effective_from=effective_from, effective_to=effective_to or None,
                     reason=reason)

    @router.post('/profiles/{profile_id}/knowledge/confirm')
    def confirm_knowledge(request: Request, profile_id: str, body: ConfirmKnowledge):
        return _call(service(request).commit_knowledge, profile_id,
                     stage_id=body.stage_id, reason=body.reason)

    @router.post('/profiles/{profile_id}/preview')
    def preview(request: Request, profile_id: str, body: AnalysisScope):
        return _call(service(request).preview, profile_id, **body.model_dump())

    @router.post('/profiles/{profile_id}/analyze')
    def analyze(request: Request, profile_id: str, body: Analyze):
        return _call(service(request).analyze, profile_id, **body.model_dump())

    @router.get('/runs/{run_id}')
    def frozen_run(request: Request, run_id: str):
        return service(request).get_run(run_id)

    @router.get('/runs/{run_id}/export/{format}')
    def export_report(request: Request, run_id: str, format: Literal['docx', 'pdf']):
        try:
            content = service(request).export_report(run_id, format)
        except RuntimeError:
            raise HTTPException(status_code=503, detail='报告渲染资源不可用，冻结分析未改变') from None
        mime = ('application/vnd.openxmlformats-officedocument.wordprocessingml.document'
                if format == 'docx' else 'application/pdf')
        return Response(content=content, media_type=mime,
                        headers={'Content-Disposition': f'attachment; filename="{run_id}.{format}"',
                                 'Cache-Control': 'no-store'})

    @router.post('/runs/{run_id}/task-draft')
    def task_draft(request: Request, run_id: str, body: DraftTask):
        return service(request).task_draft(run_id, **body.model_dump())

    return router

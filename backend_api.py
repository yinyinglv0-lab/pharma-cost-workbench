"""Authenticated HTTP boundary for the governed cost workbench.

The same Application and domain services are used by Streamlit. Authentication
failures are never converted to local fallback queries or caller-supplied actors.
"""
from __future__ import annotations
from calendar import monthrange
from datetime import date
import json
import logging
import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from dashboard.data_layer import build_dashboard_data, discover_months, discover_products
from enterprise.application import Application
from enterprise.security import api_principal, require, AuthenticationError
from enterprise.task_workflow import TaskConflict, TaskNotFound
from enterprise.knowledge import VersionConflict
from enterprise.operations import MaintenanceError
from paths import MANAGED_DIR

app = FastAPI(title='制药企业成本智能分析系统 API', version='2.0.0')
origins = [x.strip() for x in os.environ.get('COST_CORS_ORIGINS', '').split(',') if x.strip()]
if origins:
    app.add_middleware(CORSMiddleware, allow_origins=origins,
                       allow_methods=['GET', 'POST'], allow_headers=['Authorization', 'Content-Type'])
_LOG = logging.getLogger('project4.api')
if not _LOG.handlers:
    _LOG.addHandler(logging.StreamHandler())
_LOG.setLevel(logging.INFO)
_LOG.propagate = False
from collections import Counter
from threading import Lock
_HTTP_STATUS_COUNTS = Counter()
_HTTP_METRICS_LOCK = Lock()
_STARTED = time.monotonic()
from enterprise.build_info import source_fingerprint
_BUILD_FINGERPRINT = source_fingerprint()


@app.middleware('http')
async def authenticated_request(request: Request, call_next):
    ident = uuid.uuid4().hex
    started = time.monotonic()
    request.state.request_id = ident
    try:
        if request.url.path not in ('/api/health', '/openapi.json', '/docs', '/redoc'):
            request.state.principal = api_principal(request)
        response = await call_next(request)
    except AuthenticationError as exc:
        response = JSONResponse({'detail': str(exc), 'request_id': ident}, status_code=401)
    except PermissionError as exc:
        response = JSONResponse({'detail': str(exc), 'request_id': ident}, status_code=403)
    except TaskNotFound as exc:
        response = JSONResponse({'detail': str(exc), 'request_id': ident}, status_code=404)
    except (TaskConflict, VersionConflict) as exc:
        response = JSONResponse({'detail': str(exc), 'request_id': ident}, status_code=409)
    except MaintenanceError:
        response = JSONResponse({'detail': '系统维护中，请稍后重试', 'request_id': ident}, status_code=503,
                                headers={'Retry-After': '30'})
    except ValueError as exc:
        response = JSONResponse({'detail': str(exc), 'request_id': ident}, status_code=422)
    except Exception as exc:
        # No third-party exception bodies, uploaded values or credentials in logs.
        _LOG.error(json.dumps({'request_id': ident, 'outcome': 'error', 'error_type': type(exc).__name__}))
        response = JSONResponse({'detail': '操作未完成，请按request_id检查服务日志', 'request_id': ident}, status_code=503)
    response.headers['X-Request-ID'] = ident
    response.headers['Cache-Control'] = 'no-store'
    with _HTTP_METRICS_LOCK:
        _HTTP_STATUS_COUNTS[str(response.status_code)] += 1
    _LOG.info(json.dumps({'request_id': ident, 'method': request.method,
                          'route': getattr(request.scope.get('route'), 'path', 'unmatched'),
                          'status': response.status_code, 'duration_ms': round((time.monotonic()-started)*1000)}))
    return response


def service(request):
    return Application(request.state.principal)


def _scope_spec(d, product, specification=None):
    frame = d.get('cost26')
    if frame is None or frame.empty or product not in set(frame['产品名称']):
        raise HTTPException(404, detail='产品不存在或无权访问')
    candidates = frame.loc[frame['产品名称'] == product, '产品规格'].dropna().unique()
    if len(candidates) > 1 and not specification:
        raise HTTPException(422, detail='该产品有多个规格，请提供specification')
    specification = specification or candidates[0]
    if specification not in candidates:
        raise HTTPException(404, detail='产品规格不存在')
    return {k: frame[(frame['产品名称'] == product) & (frame['产品规格'] == specification)].copy()
            if not frame.empty and '产品规格' in frame else frame for k, frame in d.items()}


def _disp_mapping(data):
    out = dict(data)
    for key in ('series', 'mom', 'yoy', 'budget_var', 'contribution'):
        out[key] = [{k: round(v, 2) if isinstance(v, float) else v for k, v in row.items()}
                    for row in data.get(key, [])]
    return out


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': '2.0.0', 'source_fingerprint': _BUILD_FINGERPRINT,
            'validation': 'performed_per_authorized_request'}


@app.get('/api/system/status')
def system_status(request: Request):
    require(request.state.principal, 'system.read')
    from enterprise.operations import operations_metrics
    from enterprise.model_gateway import configuration
    from paths import DATA_DIR
    with _HTTP_METRICS_LOCK:
        counts = dict(_HTTP_STATUS_COUNTS)
    return {'uptime_seconds': round(time.monotonic()-_STARTED), 'http_status_counts': counts,
            'scope': '当前API进程计数；重启归零',
            'operations': operations_metrics(data_root=DATA_DIR, managed_root=MANAGED_DIR),
            'model': configuration().public()}


@app.get('/api/me')
def me(request: Request):
    from dataclasses import asdict
    return asdict(request.state.principal)


@app.get('/api/dashboard/products')
def api_products(request: Request):
    return {'products': discover_products(service(request).tables())}


@app.get('/api/dashboard/months')
def api_months(request: Request):
    return {'months': discover_months(service(request).tables())}


@app.get('/api/dashboard/{product}')
def api_dashboard(product: str, request: Request, specification: str | None = None):
    principal = request.state.principal
    require(principal, 'dashboard.read', factory='中药一厂', product=product)
    tables = _scope_spec(service(request).tables(), product, specification)
    return _disp_mapping(build_dashboard_data(product, tables))


@app.post('/api/dashboard/validate')
def api_validate(request: Request):
    tables = service(request).tables()
    from dashboard.validate import run_all_validation
    layers = run_all_validation(tables, verbose=False)
    errors = {k: v[0] for k, v in layers.items() if v[0]}
    return {'passed': not errors, 'layers': errors,
            'warnings': {k: v[1] for k, v in layers.items() if v[1]}}


class AttributionParams(StrictModel):
    product: str
    month: str = Field(pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    use_llm: bool = True
    specification: str | None = None


@app.post('/api/attribution')
def api_attribution(params: AttributionParams, request: Request):
    from attribution_gen import generate_attribution
    principal = request.state.principal
    require(principal, 'analysis.generate', factory='中药一厂', product=params.product)
    tables = _scope_spec(service(request).tables(), params.product, params.specification)
    return generate_attribution(params.product, params.month, use_llm=params.use_llm, d=tables,
                                principal=principal, root=MANAGED_DIR)


class SearchParams(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    mode: str = 'formal'
    product: str | None = None
    factory: str | None = None
    as_of: str = Field(default_factory=lambda: date.today().isoformat())
    known_at: str | None = None


@app.post('/api/search')
def api_search(params: SearchParams, request: Request):
    from enterprise.knowledge_release import get_search_engine
    principal = request.state.principal
    require(principal, 'knowledge.read', product=params.product, factory=params.factory)
    if params.mode != 'formal':
        raise HTTPException(422, detail='受控检索仅提供正式发布资料，模拟案例不进入候选')
    rows, stats = get_search_engine(repository=service(request).knowledge()).search(
        params.query, principal=principal, product=params.product, factory=params.factory,
        as_of=params.as_of, known_at=params.known_at, top_k=params.top_k)
    return {'query': params.query,
            'results': [{**row, 'source': row['meta']['filename']} for row in rows], 'stats': stats}


class ReportParams(AttributionParams):
    theme: str = '月度成本分析'
    formal: bool = True
    include_benchmark: bool = True
    focus: str | None = None
    format: str = 'docx'


def create_report(params, request):
    from report.datafill import resolve_period
    from enterprise.analysis_service import report_evidence, validated_model
    from enterprise.report_service import build_report_payload
    from enterprise.report_records import ReportRepository
    from enterprise.model_gateway import configuration
    principal = request.state.principal
    require(principal, 'report.generate', factory='中药一厂', product=params.product)
    tables = _scope_spec(service(request).report_tables(), params.product, params.specification)
    specification = params.specification or tables['cost26']['产品规格'].iloc[0]
    if params.include_benchmark:
        require(principal, 'data.read', factory='中药二厂', product=params.product)
    raw = params.model_dump(exclude={'format'})
    raw['specification'] = specification
    months = resolve_period(params.theme, params.month)[0]
    evidence = report_evidence(principal, params.product, specification, months, MANAGED_DIR)
    configured = configuration()
    payload = build_report_payload(raw, tables, evidence=evidence,
        model_fn=validated_model if params.use_llm and configured.api_key else None,
        model_version=configured.model if configured.api_key else 'not_configured',
        versions={'cost_revision': tables['cost26'].attrs.get('cost_revision'),
                  'cost_snapshot_hash': tables['cost26'].attrs.get('cost_snapshot_hash')})
    return ReportRepository(MANAGED_DIR).save(payload, actor=principal)


def report_download(record, format, principal):
    from enterprise.report_records import ReportRepository
    content = ReportRepository(MANAGED_DIR).export(record['id'], format, actor=principal)
    mime = 'application/pdf' if format == 'pdf' else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    return Response(content=content, media_type=mime, headers={
        'Content-Disposition': f'attachment; filename="{record["id"]}.{format}"',
        'X-Report-ID': record['id'], 'X-Review-Status': record['status']})


@app.post('/api/report')
def api_report(params: ReportParams, request: Request):
    return report_download(create_report(params, request), params.format, request.state.principal)


@app.post('/api/reports')
def api_create_report(params: ReportParams, request: Request):
    return create_report(params, request)


@app.get('/api/reports')
def api_reports(request: Request):
    from enterprise.report_records import ReportRepository
    return {'reports': ReportRepository(MANAGED_DIR).list(actor=request.state.principal)}


@app.get('/api/reports/{ident}')
def api_report_record(ident: str, request: Request):
    from enterprise.report_records import ReportRepository
    return ReportRepository(MANAGED_DIR).get(ident, actor=request.state.principal)


class Decision(StrictModel):
    expected_version: int = Field(ge=1)
    reason: str = ''


@app.post('/api/reports/{ident}/{action}')
def api_report_decision(ident: str, action: str, params: Decision, request: Request):
    from enterprise.report_records import ReportRepository
    repo = ReportRepository(MANAGED_DIR)
    if action == 'submit':
        return repo.submit(ident, actor=request.state.principal, expected_version=params.expected_version)
    if action in ('approve', 'reject'):
        return getattr(repo, action)(ident, actor=request.state.principal,
                                    expected_version=params.expected_version, reason=params.reason)
    raise HTTPException(404, detail='报告动作不存在')


@app.get('/api/reports/{ident}/export/{format}')
def api_report_export(ident: str, format: str, request: Request):
    return report_download(api_report_record(ident, request), format, request.state.principal)


@app.post('/api/upload/cost')
async def api_upload_cost(file: UploadFile, request: Request):
    content = await file.read(20*1024*1024 + 1)
    preview = service(request).stage_costs([(file.filename or 'unknown.csv', content)])
    return {key: value for key, value in preview.items() if key != 'tables'}


class CostCommitParams(StrictModel):
    stage_id: str
    mode: str
    reason: str = ''


@app.post('/api/upload/cost/confirm')
def api_confirm_cost(params: CostCommitParams, request: Request):
    return service(request).confirm_costs(params.stage_id, params.mode, params.reason)


@app.get('/api/audit')
def api_audit(request: Request):
    return {'rows': service(request).cost_history()}


@app.get('/api/kb/collections')
def api_kb_collections(request: Request):
    repo = service(request).knowledge()
    rows = repo.list_documents()
    return {'collections': [{'name': 'governed_documents', 'count': len(rows), 'demo_count': 0}]}


@app.get('/api/kb/chunks')
def api_kb_chunks(request: Request, offset: int = 0, limit: int = 50):
    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(422, detail='分页范围错误')
    rows = service(request).knowledge().list_documents()
    return {'total': len(rows), 'items': rows[offset:offset+limit]}


class BenchmarkParams(AttributionParams):
    specification: str


@app.post('/api/benchmark')
def api_benchmark(params: BenchmarkParams, request: Request):
    from enterprise.benchmark_ai import generate_benchmark_analysis
    from enterprise.analysis_service import report_evidence, validated_model
    from enterprise.model_gateway import configuration
    principal = request.state.principal
    for factory in ('中药一厂', '中药二厂'):
        require(principal, 'analysis.generate', factory=factory, product=params.product)
    tables = service(request).tables()
    evidence = report_evidence(principal, params.product, params.specification, [params.month], MANAGED_DIR)
    cfg = configuration()
    return generate_benchmark_analysis(params.product, params.specification, params.month, tables,
        evidence=evidence, model_fn=validated_model if cfg.api_key and params.use_llm else None,
        model_version=cfg.model if cfg.api_key else 'not_configured', use_llm=params.use_llm)


class TaskCreate(StrictModel):
    payload: dict
    generate_with_model: bool = False


@app.post('/api/tasks')
def api_create_task(params: TaskCreate, request: Request):
    repo = service(request).tasks()
    if params.generate_with_model:
        return repo.generate(params.payload, actor=request.state.principal)
    return repo.create(params.payload, actor=request.state.principal)


@app.get('/api/tasks')
def api_tasks(request: Request):
    return {'tasks': service(request).tasks().list(actor=request.state.principal)}


@app.get('/api/tasks/summary')
def api_task_summary(request: Request):
    return service(request).tasks().summary(actor=request.state.principal)


@app.get('/api/tasks/{ident}')
def api_task(ident: str, request: Request):
    return service(request).tasks().get(ident, actor=request.state.principal)


class TaskEdit(StrictModel):
    changes: dict
    expected_version: int = Field(ge=1)


@app.post('/api/tasks/{ident}/edit')
def api_edit_task(ident: str, params: TaskEdit, request: Request):
    return service(request).tasks().update(ident, params.changes, actor=request.state.principal,
                                          expected_version=params.expected_version)


@app.post('/api/tasks/{ident}/{action}')
def api_task_action(ident: str, action: str, params: Decision, request: Request):
    repo, principal = service(request).tasks(), request.state.principal
    kw = {'actor': principal, 'expected_version': params.expected_version}
    if action in ('submit', 'enqueue'):
        return getattr(repo, action)(ident, **kw)
    if action == 'approve':
        return repo.approve(ident, comment=params.reason, **kw)
    if action == 'reject':
        return repo.reject(ident, reason=params.reason, **kw)
    if action in ('dispatch', 'sync'):
        from enterprise.rpa_client import RPAClient
        with RPAClient() as client:
            return repo.dispatch(client, actor=principal, task_id=ident) if action == 'dispatch' else repo.sync(ident, client, actor=principal)
    raise HTTPException(404, detail='任务动作不存在')

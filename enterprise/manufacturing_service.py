"""Authenticated cross-industry application: canonical data -> common RAG/model -> draft.

No service method selects roles, modifies the legacy pharmacy deployment, bypasses
model approval, silently degrades dense retrieval, or approves/sends a task. Saved
runs are immutable; task creation revalidates current scopes and knowledge access.
"""
from __future__ import annotations

from calendar import monthrange
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from enterprise.manufacturing_repository import ManufacturingRepository, canonical, digest
from enterprise.operations import write_guard
from enterprise.security import require


class ManufacturingService:
    def __init__(self, root, principal):
        self.root = Path(root)
        self.principal = principal
        self.repository = ManufacturingRepository(root, principal)
        self.db = self.root / 'manufacturing_analyses.db'

    def stage_knowledge(self, profile_id, *, content, filename, title, product_ids,
                        effective_from, effective_to=None, category, reason):
        """Bind uploaded references to a reviewed config without widening ACLs.

        Commit and dense publication deliberately remain the existing governed
        knowledge workflow. A pending catalog record is never used by analysis.
        """
        require(self.principal, 'knowledge.stage')
        config = self.repository.configuration(profile_id)
        profile = config['profile']
        if (not isinstance(product_ids, list) or not product_ids
                or any(not isinstance(item, str) for item in product_ids)
                or len(set(product_ids)) != len(product_ids)):
            raise ValueError('须选择不重复的受控产品标识')
        selected = [row for row in profile['products'] if row['id'] in product_ids]
        if len(selected) != len(product_ids):
            raise ValueError('产品未在本领域配置登记')
        if category not in {'process', 'formula', 'equipment', 'industry_benchmark', 'market_prices', 'other'}:
            raise ValueError('资料类别不在受控列表')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ValueError('请提供不超过1000字的复核理由')
        if category in {'process', 'formula', 'equipment'} and len(selected) != 1:
            raise ValueError('机制文档须逐产品登记，避免多产品段落错配；不能以权限范围代替内容适用性')
        for factory in profile['factories'].values():
            for product in selected:
                require(self.principal, 'knowledge.stage', factory=factory, product=product['name'])
        from enterprise.domain_vocabulary import build_graph_vocabulary
        from enterprise.domain_profiles import profile_fingerprint
        from enterprise.knowledge import Repository
        metadata = {'manufacturing_domain_profile': profile,
            'manufacturing_profile_sha256': profile_fingerprint(profile),
            'manufacturing_config_hash': config['config_hash'],
            'graph_vocabulary': build_graph_vocabulary(profile),
            'graph_vocabulary_review': {'reason': reason.strip()},
            'scope_specifications': sorted({row['specification'] for row in selected}),
            'evidence_role': 'context_only' if category == 'other' else 'document_basis',
            'authority': 'operator_reviewed', 'data_classification': profile['data_classification'],
            'limitations': list(profile.get('limitations', [])), 'known_conflicts': []}
        category_name = {'process': '生产工艺', 'formula': '产品配方', 'equipment': '设备参考',
                         'industry_benchmark': '行业基准', 'market_prices': '市场参考', 'other': '其他'}[category]
        pending = Repository(self.root, principal=self.principal).stage(content, filename, title,
            [row['name'] for row in selected], effective_from, category_name, self.principal.user_id,
            effective_to=effective_to, scope_factories=list(profile['factories'].values()), visibility='scoped',
            metadata=metadata)
        return {**pending, 'manufacturing_profile_id': profile_id, 'config_hash': config['config_hash'],
                'publication_status': 'not_published', 'model_called': False}

    def commit_knowledge(self, profile_id, *, stage_id, reason):
        require(self.principal, 'knowledge.publish')
        if not isinstance(stage_id, str) or not re.fullmatch(r'[a-f0-9]{32}', stage_id):
            raise ValueError('知识暂存标识无效')
        from enterprise.knowledge import Repository, KnowledgeError
        from enterprise.manufacturing_repository import ManufacturingConflict
        with write_guard(self.root):
            config = self.repository.configuration(profile_id)
            knowledge = Repository(self.root, principal=self.principal)
            connection = knowledge._connect()
            if connection is None:
                raise KnowledgeError('知识暂存不存在')
            try:
                row = connection.execute('SELECT payload,payload_sha256 FROM stages WHERE stage_id=?',
                                         (stage_id,)).fetchone()
            finally:
                connection.close()
            if row is None:
                raise KnowledgeError('知识暂存不存在')
            if hashlib.sha256(row['payload'].encode('utf-8')).hexdigest() != row['payload_sha256']:
                raise KnowledgeError('知识暂存完整性校验失败')
            payload = json.loads(row['payload'])
            metadata = payload.get('business_metadata') or {}
            if (metadata.get('manufacturing_domain_profile') != config['profile']
                    or metadata.get('manufacturing_config_hash') != config['config_hash']):
                raise ManufacturingConflict('知识暂存的领域配置已变化或不匹配，请重新登记预览')
            for factory in payload.get('scope_factories', []):
                for product in payload.get('scope_products', []):
                    require(self.principal, 'knowledge.publish', factory=factory, product=product)
            version = knowledge.commit(stage_id, self.principal.user_id, reason)
        return {'version': version, 'manufacturing_profile_id': profile_id,
                'config_hash': config['config_hash'], 'publication_status': 'catalog_only',
                'index_publication_required': True, 'model_called': False}

    def _authorize_analysis(self, profile, product, *, action='analysis.generate'):
        for factory in profile['factories'].values():
            require(self.principal, action, factory=factory, product=product)
            require(self.principal, 'data.read', factory=factory, product=product)

    def preview(self, profile_id, *, product, specification, month):
        snapshot = self.repository.current(profile_id)
        self._authorize_analysis(snapshot['profile'], product)
        facts = snapshot['runtime'].analysis(product=product, specification=specification, month=month)
        from enterprise.manufacturing_projection import project_manufacturing_analysis
        projected = project_manufacturing_analysis(facts, snapshot['profile'])
        return {'schema_version': 'manufacturing-preview/1', 'profile_id': profile_id,
                'revision': snapshot['revision'], 'data_hash': snapshot['sha256'],
                'config_hash': snapshot['config_hash'], 'facts': facts, 'projection': projected,
                'effects': {'model_calls': 0, 'writes': False, 'task_created': False, 'task_sent': False}}

    def analyze(self, profile_id, *, product, specification, month, use_llm=False):
        if type(use_llm) is not bool:
            raise ValueError('use_llm必须是布尔值')
        preview = self.preview(profile_id, product=product, specification=specification, month=month)
        config = self.repository.configuration(profile_id)
        if config['config_hash'] != preview['config_hash']:
            raise ValueError('读取期间领域配置变化，请重新分析')
        profile, facts, projection = config['profile'], preview['facts'], preview['projection']
        from enterprise.analysis_service import report_evidence
        from enterprise.knowledge import KnowledgeError
        # The same production controlled retriever enforces ACL/temporal scope,
        # published dense+BM25 readiness and exact raw-source citation metadata.
        documents, diagnostics = [], {'degraded': True, 'reason': 'not_requested'}
        retrieval_error = None
        try:
            documents = report_evidence(self.principal, product, specification, [month], self.root,
                facts=projection['attribution_payload']['facts'], require_hybrid=True, domain_profile=profile)
            diagnostics = deepcopy(documents.diagnostics)
            previous = facts['scope'].get('previous_month')
            if previous:
                prior = report_evidence(self.principal, product, specification, [previous], self.root,
                    facts=projection['attribution_payload']['facts'], purposes=['market_reference'],
                    require_hybrid=True, domain_profile=profile)
                if prior.diagnostics.get('degraded'):
                    raise KnowledgeError('基期市场检索降级，禁止拼接不完整的正式证据')
                if prior.diagnostics.get('release_id') != diagnostics.get('release_id'):
                    raise KnowledgeError('检索期间知识发布版本变化，禁止拼接两代证据')
                seen = {row['id'] for row in documents}
                documents.extend(row for row in prior if row['id'] not in seen)
                diagnostics['prior_market_queries'] = deepcopy(prior.diagnostics)
            if diagnostics.get('degraded'):
                raise KnowledgeError('混合检索降级，禁止正式模型分析')
        except (PermissionError, KnowledgeError, ValueError) as exc:
            if isinstance(exc, PermissionError):
                raise
            retrieval_error = type(exc).__name__
            documents = []
            diagnostics = {'degraded': True, 'reason': 'hybrid_unavailable', 'error_type': retrieval_error,
                           'formal_model_blocked': True}
        from enterprise.industry_benchmark import build_canonical_industry_comparison
        from enterprise.evidence_references import market_reading_rows
        comparison = build_canonical_industry_comparison(facts, documents, profile)
        market = market_reading_rows(documents, product, specification, month, profile=profile)
        results = {}
        for kind in ('attribution', 'benchmark'):
            payload = deepcopy(projection[kind + '_payload'])
            sources = deepcopy(projection['sources'][kind]) + deepcopy(list(documents))
            payload['industry_comparison'] = comparison
            payload['market_reference'] = {'rows': market}
            # Server-owned contract, not a field accepted from uploaded data.
            payload['prose_mode'] = 'bound-numeric-prose/1'
            model_run, candidate = {'attempts': [], 'model_calls': [], 'provider_call_count': 0}, None
            status = 'deterministic_requested' if not use_llm else 'retrieval_unavailable' if retrieval_error else 'model_not_called'
            no_difference = self._no_difference(kind, payload, sources, projection['narrative_config'])
            if kind == 'attribution' and not payload['facts'].get('available'):
                status = 'comparison_unavailable'
            elif no_difference:
                status = 'no_difference'
            elif use_llm and not retrieval_error:
                model_run, candidate, status = self._model(kind, payload, sources)
            from enterprise.analysis_narrative import build_attribution_narrative, build_benchmark_narrative
            if kind == 'attribution':
                narrative = build_attribution_narrative(payload, candidate, sources,
                    detailed=True, industry_comparison=comparison, config=projection['narrative_config'])
            else:
                narrative = build_benchmark_narrative(payload['facts'], sources, candidate,
                    industry_comparison=comparison, config=projection['narrative_config'],
                    prose_mode=payload.get('prose_mode'))
            results[kind] = {'payload': payload, 'sources': sources, 'narrative': narrative,
                             'generation_status': status, 'used_llm': candidate is not None,
                             'model_explanations': candidate, 'model_run': model_run}
        # Do not silently bind completed work to a newer configuration/data revision.
        current = self.repository.current(profile_id)
        if (current['revision'] != preview['revision'] or current['sha256'] != preview['data_hash']
                or current['config_hash'] != preview['config_hash']):
            raise ValueError('分析期间数据或配置变更，结果未保存；请重新分析')
        from enterprise.build_info import source_fingerprint, deployment_fingerprint
        result = {'schema_version': 'manufacturing-analysis/1', 'profile_id': profile_id,
            'revision': preview['revision'], 'data_hash': preview['data_hash'], 'config_hash': preview['config_hash'],
            'profile': profile, 'scope': facts['scope'], 'measurement': facts['measurement'],
            'data_classification': facts['data_classification'], 'facts': facts, 'analyses': results,
            'industry_comparison': comparison, 'market_reference': market, 'charts': projection['charts'],
            'retrieval_diagnostics': diagnostics, 'source_fingerprint': source_fingerprint(),
            'deployment_fingerprint': deployment_fingerprint(), 'created': datetime.now(timezone.utc).isoformat(),
            'review_status': 'needs_human_review', 'effects': {'task_created': False, 'task_sent': False}}
        return self._save(result)

    @staticmethod
    def _no_difference(kind, payload, sources=None, config=None):
        from decimal import Decimal
        facts = payload['facts']
        if kind == 'benchmark':
            rows = facts.get('elements', [])
            return bool(rows) and all(Decimal(str(row.get('unit_gap', 0))) == 0 for row in rows)
        if not facts.get('available'):
            return False
        # The shared engine accounts for offsetting detail/labor/physical effects;
        # zero aggregate bridges alone are not proof that no analysis is needed.
        from enterprise.analysis_narrative import build_attribution_narrative
        sections = build_attribution_narrative(payload, None, sources, config=config)['sections']
        return bool(sections) and all(row.get('claim_type') == 'no_difference' for row in sections)

    @staticmethod
    def _model(kind, payload, sources):
        from attribution_runtime import run_stage, StageExecutionError, sanitize_model_calls, prepare_model_stage
        from enterprise.prose_contract import model_stage_budget
        stage_budget = model_stage_budget(payload)
        run = {'attempts': [], 'correction': {'attempted': False, 'status': 'not_requested'},
               'model_calls': [], 'provider_call_count': 0, 'execution': 'bounded_worker',
               'hard_budget_seconds': stage_budget}
        try:
            stage_args, stage_budget = prepare_model_stage(payload, sources, task=kind)
            run.update(hard_budget_seconds=stage_budget, model_identity=stage_args.identity)
            response = run_stage('model', stage_args, timeout=stage_budget)
            if kind == 'attribution':
                from attribution_gen import M2_MODEL_RUN_SCHEMA, model_diagnostics, _model_context
                expected = M2_MODEL_RUN_SCHEMA
            else:
                from enterprise.benchmark_ai import MODEL_RUN_SCHEMA, validate_explanations_structured, grouped_model_context
                expected = MODEL_RUN_SCHEMA
            if not isinstance(response, dict) or response.get('schema') != expected:
                raise ValueError('受控模型返回结构无效')
            run.update({key: deepcopy(response[key]) for key in ('attempts', 'correction') if key in response})
            run['provider_call_count'] = len(run['attempts'])
            run['model_calls'] = [trace for attempt in run['attempts']
                                  for trace in sanitize_model_calls(attempt.get('model_calls'))]
            candidate = response.get('candidate')
            if response.get('failure_type'):
                run['failure_type'] = response['failure_type']
                return run, None, 'model_unavailable'
            from enterprise.analysis_contract import legacy_errors
            if kind == 'attribution':
                context = _model_context(payload, sources, include_numeric=True)
                diagnostics = model_diagnostics(candidate, sources, payload['facts'], context=context)
            else:
                context = grouped_model_context(payload, sources, include_numeric=True)
                diagnostics = validate_explanations_structured(candidate, sources, payload['facts'], context=context)
            if diagnostics:
                run['validation_errors'] = legacy_errors(diagnostics)
                run['validation_diagnostics'] = diagnostics
                for attempt in run['attempts']:
                    attempt['used'] = False
                return run, None, 'model_rejected'
            return run, candidate, 'model_validated'
        except Exception as exc:
            if isinstance(exc, StageExecutionError):
                run.update(deepcopy(exc.model_run or {}))
            run['failure_type'] = type(exc).__name__
            for attempt in run.get('attempts', []):
                attempt['used'] = False
            run['provider_call_count'] = len(run.get('attempts', []))
            run['model_calls'] = [trace for attempt in run.get('attempts', [])
                                  for trace in sanitize_model_calls(attempt.get('model_calls'))]
            return run, None, 'model_unavailable'

    def _connect(self, create=False):
        if not create and not self.db.exists():
            raise ValueError('分析记录不存在或不可用')
        self.root.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        if create:
            con.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL, '
                        'actor TEXT NOT NULL,created TEXT NOT NULL,sha256 TEXT NOT NULL,payload TEXT NOT NULL)')
            for operation in ('UPDATE', 'DELETE'):
                con.execute(f"CREATE TRIGGER IF NOT EXISTS runs_no_{operation.lower()} BEFORE {operation} ON runs "
                            "BEGIN SELECT RAISE(ABORT, 'immutable manufacturing analysis'); END")
            con.commit()
        return con

    def _save(self, result):
        self._authorize_analysis(result['profile'], result['scope']['product'])
        identifier = 'ma_' + uuid4().hex
        sha = digest(result)
        with write_guard(self.root):
            current = self.repository.current(result['profile_id'])
            if (current['revision'] != result['revision'] or current['sha256'] != result['data_hash']
                    or current['config_hash'] != result['config_hash']):
                raise ValueError('保存前数据或配置已变更，请重新分析')
            from enterprise.knowledge import Repository
            knowledge = Repository(self.root, principal=self.principal)
            versions = {source['version_id'] for analysis in result['analyses'].values()
                        for source in analysis['sources'] if source.get('version_id')}
            for version in versions:
                if not knowledge.get(version_id=version, principal=self.principal):
                    raise PermissionError('保存前知识引用已不可访问')
            with closing(self._connect(create=True)) as con, con:
                con.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)',
                            (identifier, result['profile_id'], self.principal.user_id, result['created'], sha, canonical(result)))
        return {**result, 'analysis_run_id': identifier, 'analysis_hash': sha}

    def get_run(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'ma_[a-f0-9]{32}', identifier):
            raise ValueError('分析标识无效')
        with closing(self._connect()) as con:
            row = con.execute('SELECT * FROM runs WHERE id=?', (identifier,)).fetchone()
        if row is None:
            raise ValueError('分析记录不存在或不可用')
        result = json.loads(row['payload'])
        if digest(result) != row['sha256']:
            raise ValueError('冻结分析完整性校验失败')
        # Saved runs include the full frozen profile/BOM, not just selected rows.
        # Require every scope in that profile before returning that metadata.
        self.repository._authorize(result['profile'], 'data.read')
        self.repository._authorize(result['profile'], 'report.read')
        from enterprise.knowledge import Repository
        knowledge = Repository(self.root, principal=self.principal)
        versions = {source['version_id'] for analysis in result['analyses'].values()
                    for source in analysis['sources'] if source.get('version_id')}
        for version_id in versions:
            if not knowledge.get(version_id=version_id, principal=self.principal):
                raise PermissionError('冻结分析知识引用权限已变化')
        return {**result, 'analysis_run_id': identifier, 'analysis_hash': row['sha256']}

    def export_report(self, identifier, format):
        """Export only an authorized frozen run, never regenerate its analysis."""
        from enterprise.manufacturing_report import export_manufacturing_report
        result = self.get_run(identifier)
        content = export_manufacturing_report(result, format)
        # Rendering may take time. Recheck grants/revocations before releasing bytes.
        self.get_run(identifier)
        return content

    def task_draft(self, identifier, *, kind, element):
        # Reauthorization and draft persistence share the revocation/write lock.
        with write_guard(self.root):
            return self._task_draft(identifier, kind=kind, element=element)

    def _task_draft(self, identifier, *, kind, element):
        if kind not in {'attribution', 'benchmark'} or element not in {'材料', '人工', '制费'}:
            raise ValueError('任务须选定归因/对标及一个成本要素')
        result = self.get_run(identifier)
        require(self.principal, 'task.create', product=result['scope']['product'])
        for factory in result['profile']['factories'].values():
            require(self.principal, 'task.create', factory=factory, product=result['scope']['product'])
        analysis = result['analyses'][kind]
        section = next((row for row in analysis['narrative']['sections'] if row.get('element') == element), None)
        if not section or section.get('claim_type') == 'no_difference' or analysis['generation_status'] == 'no_difference':
            raise ValueError('此要素没有待核查差异，不生成整改任务')
        month = result['scope']['month']
        source_ids = list(dict.fromkeys(section.get('evidence_ids', [])))
        if not source_ids:
            raise ValueError('任务缺少可追溯分析证据')
        suggestion = section.get('recommendation') or section.get('immediate_action') or ''
        from enterprise.task_workflow import TaskRepository
        payload = {'task_title': '核查' + result['scope']['product'] + element + '成本差异',
            'assignee': {'name': '', 'department': '', 'role': ''}, 'priority': 'medium', 'deadline': '',
            'factories': list(result['profile']['factories'].values()), 'evidence_ids': source_ids,
            'analysis_run_id': identifier,
            'evidence_hashes': {source['id']: digest(source) for source in analysis['sources']
                                if source['id'] in source_ids},
            'source': {'analysis_type': '跨厂对标' if kind == 'benchmark' else '月度成本分析',
                       'analysis_month': month, 'product': result['scope']['product'],
                       'finding': section.get('numeric_explanation') or section.get('fact') or section['text']},
            'suggestion': suggestion + '\n' + analysis['narrative']['followup_criteria']}
        from report.datafill import resolve_period
        months, _, _, label = resolve_period('专题分析' if kind == 'benchmark' else '月度成本分析', month)
        payload['analysis_period'] = {'months': months, 'label': label, 'coverage': 'full_period'}
        # Same governed task repository; ONLY draft creation, never auto-approval.
        task = TaskRepository(self.root).create(payload, actor=self.principal)
        return {'task': task, 'analysis_run_id': identifier, 'analysis_hash': result['analysis_hash'],
                'task_source': 'frozen_manufacturing_analysis', 'sent': False, 'approved': False}

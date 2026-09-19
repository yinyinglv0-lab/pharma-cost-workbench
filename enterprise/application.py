"""Application boundary shared by Streamlit and HTTP handlers.

Repositories are internal persistence components. All user-visible reads and
writes go through this service (or a domain service accepting Principal).
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from paths import MANAGED_DIR
from enterprise.cost_imports import CostRepository, frames, validate
from enterprise.security import Principal, require, filter_tables, can
from enterprise.operations import guarded_write


class Application:
    def __init__(self, principal: Principal, root=None, *, baseline_loader=None):
        if not isinstance(principal, Principal):
            raise PermissionError('业务服务需要已认证用户')
        self.principal = principal
        self.root = Path(root) if root else MANAGED_DIR
        self.costs = CostRepository(self.root, baseline_loader=baseline_loader)

    def tables(self):
        require(self.principal, 'data.read')
        snapshot = self.costs.current()
        result = filter_tables(self.principal, frames(snapshot['tables']))
        for frame in result.values():
            frame.attrs['cost_revision'] = snapshot['revision']
            frame.attrs['cost_snapshot_hash'] = snapshot['hash']
        errors, warnings = validate({k: json.loads(v.to_json(orient='records', force_ascii=False))
                                     for k, v in result.items()})
        if errors:
            raise ValueError('当前授权数据校验失败：' + '；'.join(errors[:10]))
        return result

    def report_tables(self):
        """Add only relevant market-reference rows to the authorized cost snapshot."""
        require(self.principal, 'report.generate')
        data = self.tables()
        from paths import DATA_DIR
        import pandas as pd
        import hashlib
        market = DATA_DIR / '药材市场价格行情_2026年上半年.csv'
        frame = pd.read_csv(market) if market.is_file() else pd.DataFrame()
        if not frame.empty:
            frame['_source_file'] = str(market.resolve())
            frame['_source_hash'] = hashlib.sha256(market.read_bytes()).hexdigest()
            frame['_source_row'] = list(range(2, len(frame)+2))
            frame['_source_sheet'] = 'CSV'
            frame.attrs.update(source_file=str(market.resolve()), year=2026)
            materials = data.get('material', pd.DataFrame())
            names = set(materials['原材料名称']) if '原材料名称' in materials else set()
            if '*' not in self.principal.products:
                frame = frame.loc[frame['药材名称'].isin(names)].copy()
        data['market'] = frame
        data['cost26_2'] = data.get('erchang26', pd.DataFrame())
        return data

    def current(self):
        require(self.principal, 'data.read')
        current = self.costs.current()
        scoped = filter_tables(self.principal, frames(current['tables']))
        current['tables'] = {k: json.loads(v.to_json(orient='records', force_ascii=False))
                             for k, v in scoped.items()}
        return current

    def _authorize_changes(self, preview, action):
        require(self.principal, action)
        for change in preview.get('changes', []):
            key = change.get('key', {})
            if not key.get('工厂') or not key.get('产品名称'):
                raise PermissionError('修订缺少工厂或产品范围')
            require(self.principal, action, factory=key['工厂'], product=key['产品名称'])

    def _stage(self, stage_id):
        if not self.costs.db.exists():
            raise ValueError('待确认批次不存在')
        with closing(self.costs._connect()) as con:
            row = con.execute('SELECT * FROM stages WHERE id=?', (stage_id,)).fetchone()
        if row is None:
            raise ValueError('待确认批次不存在')
        result = json.loads(row['payload'])
        result['submitted_by'] = row['actor']
        result['status'] = row['status']
        return result

    @guarded_write
    def stage_costs(self, files):
        require(self.principal, 'data.stage')
        # Preview is persisted for auditing but never publishes data. Reject an
        # unauthorized batch before sending preview contents back to the caller.
        preview = self.costs.stage(files, self.principal.user_id, principal=self.principal)
        try:
            self._authorize_changes(preview, 'data.stage')
        except PermissionError:
            with closing(self.costs._connect()) as con, con:
                con.execute("UPDATE stages SET status='rejected' WHERE id=?", (preview['stage_id'],))
            raise
        return self.preview_costs(preview['stage_id'])

    def preview_costs(self, stage_id):
        preview = self._stage(stage_id)
        action = 'data.confirm' if can(self.principal, 'data.confirm') else 'data.stage'
        self._authorize_changes(preview, action)
        if action == 'data.stage' and preview['submitted_by'] != self.principal.user_id:
            raise PermissionError('分析员只能查看自己的待确认批次')
        preview['tables'] = {k: json.loads(v.to_json(orient='records', force_ascii=False))
                             for k, v in filter_tables(self.principal, frames(preview['tables'])).items()}
        if '*' not in self.principal.products or '*' not in self.principal.factories:
            # Full-table reconciliation can mention another product. Keep its
            # fail-closed outcome, but expose detail only for the scoped view.
            scoped_errors, scoped_warnings = validate(preview['tables'])
            preview['errors'] = scoped_errors or (['全量批次存在其他校验问题，请由有权主管复核'] if preview['errors'] else [])
            preview['warnings'] = scoped_warnings
        return preview

    def pending_costs(self):
        require(self.principal, 'data.confirm')
        if not self.costs.db.exists():
            return []
        with closing(self.costs._connect()) as con:
            rows = con.execute("SELECT id FROM stages WHERE status='preview' ORDER BY created DESC").fetchall()
        permitted = []
        for row in rows:
            try:
                preview = self.preview_costs(row['id'])
                permitted.append({k: preview[k] for k in ('stage_id', 'submitted_by', 'base_revision',
                                                            'business_periods', 'counts', 'errors')})
            except PermissionError:
                continue
        return permitted

    @guarded_write
    def confirm_costs(self, stage_id, mode, reason=''):
        require(self.principal, 'data.confirm')
        preview = self._stage(stage_id)
        self._authorize_changes(preview, 'data.confirm')
        # Local demonstration has a single OS identity. Enterprise approvals
        # require independent submitter/approver by default.
        if self.principal.auth_method != 'local_os_demo' and preview['submitted_by'] == self.principal.user_id:
            raise PermissionError('企业模式由另一位财务主管复核发布，提交人不可自批')
        from enterprise.periods import PeriodRepository
        PeriodRepository(self.root).assert_open(preview['changes'])
        return self.costs.commit(stage_id, self.principal.user_id, mode, reason)

    def cost_history(self):
        require(self.principal, 'audit.read')
        result = []
        if not self.costs.db.exists():
            return result
        with closing(self.costs._connect()) as con:
            rows = con.execute('SELECT * FROM revisions ORDER BY id DESC').fetchall()
        for row in rows:
            changes = json.loads(row['changes'])
            try:
                self._authorize_changes({'changes': changes}, 'audit.read')
            except PermissionError:
                continue
            result.append({k: row[k] for k in ('id', 'created', 'actor', 'reason', 'hash')})
        return result

    def knowledge(self):
        from enterprise.knowledge import Repository
        return Repository(self.root, principal=self.principal)

    def tasks(self):
        from enterprise.task_workflow import TaskRepository
        return TaskRepository(self.root)

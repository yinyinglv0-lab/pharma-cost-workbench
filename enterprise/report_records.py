"""Immutable report runs, independent approval and reproducible signed exports."""
from __future__ import annotations
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from paths import MANAGED_DIR
from enterprise.operations import guarded_write
from enterprise.security import require, can


def now():
    return datetime.now(timezone.utc).isoformat()


def frozen_export_payload(payload, record):
    """Apply a decision without mutating the source; retain historical overlays.

    Renderer 1/2 used a specific three-position statement overlay. Its bytes are
    part of the signed render hash, so the readable single-statement rule applies
    only to new renderer-3 payloads. Cached historical artifacts are never rebuilt.
    """
    from report.model import digest, verify_payload
    verify_payload(payload)
    exported = deepcopy(payload)
    ident = record['id']
    approved = record['status'] == 'approved'
    modern = payload['versions']['renderer']['version'] == 'shared-docx-reportlab/3.0'
    statement = ('已审核签发' if approved else '草稿／未签发') + f" · 报告{ident} · 版本{record['version']}"
    if approved:
        timestamp = record['approved']
        if modern and timestamp:
            timestamp = datetime.fromisoformat(timestamp).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        statement += (f" · 审核账号{record['approver']} · 审核时间{timestamp} · 意见{record['reason']}" if modern else
                      f" · 审核人{record['approver']} · 审核时间{record['approved']} · 意见{record['reason']}")
    elif modern:
        statement += ' · ' + {'draft': '待提交', 'submitted': '待审核', 'rejected': '已退回'}.get(record['status'], record['status'])
    exported['review_status'] = record['status']
    exported['approval'] = {'status': record['status'], 'approver': record['approver'],
                            'approved_at': record['approved'], 'reason': record['reason'],
                            'source_payload_hash': payload['frozen_hash'], 'decision_version': record['version']}
    if modern:
        slots = [block for block in exported['blocks'] if block.get('role') == 'approval_status']
        if len(slots) != 1:
            raise ValueError('新版本报告须有唯一审核状态展示位置')
        slots[0]['text'] = statement
    else:
        for block in exported['blocks']:
            if block.get('text') == '完整期间报告／待专业审核':
                block['text'] = statement
            if block.get('kind') == 'table':
                for cells in block.get('rows', []):
                    if cells and cells[0] == '审核状态' and len(cells) > 1:
                        cells[1] = statement
        exported['blocks'].insert(1, {'kind': 'paragraph', 'text': statement})
    exported.pop('frozen_hash', None)
    exported['frozen_hash'] = digest(exported)
    return exported


class ReportRepository:
    def __init__(self, root=None):
        self.root = Path(root) if root else MANAGED_DIR
        self.db = self.root / 'reports.db'

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db, timeout=15)
        con.row_factory = sqlite3.Row
        con.executescript('''
            CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
                product TEXT NOT NULL, factories TEXT NOT NULL, creator TEXT NOT NULL,
                created TEXT NOT NULL, hash TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL, version INTEGER NOT NULL, approver TEXT, approved TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS report_events(id INTEGER PRIMARY KEY, report_id TEXT NOT NULL,
                ts TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS report_artifacts(report_id TEXT, format TEXT, version INTEGER,
                hash TEXT NOT NULL, content BLOB NOT NULL, created TEXT NOT NULL,
                PRIMARY KEY(report_id,format,version));
            CREATE TRIGGER IF NOT EXISTS immutable_report_payload BEFORE UPDATE OF payload,hash,product,factories,creator,created,tenant ON reports
                BEGIN SELECT RAISE(ABORT,'report inputs and generated content are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_report_delete BEFORE DELETE ON reports
                BEGIN SELECT RAISE(ABORT,'reports are retained'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_report_event_update BEFORE UPDATE ON report_events
                BEGIN SELECT RAISE(ABORT,'report decisions are append only'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_report_event_delete BEFORE DELETE ON report_events
                BEGIN SELECT RAISE(ABORT,'report decisions are append only'); END;
        ''')
        return con

    def _authorize(self, row, actor, action='report.read'):
        require(actor, action, product=row['product'])
        if actor.tenant_id != row['tenant']:
            raise PermissionError('报告组织不匹配')
        for factory in json.loads(row['factories']):
            require(actor, action, factory=factory, product=row['product'])

    def _load(self, con, ident, actor, action='report.read'):
        row = con.execute('SELECT * FROM reports WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise ValueError('报告不存在')
        self._authorize(row, actor, action)
        payload = json.loads(row['payload'])
        from report.model import verify_payload
        verify_payload(payload)
        if payload['frozen_hash'] != row['hash']:
            raise ValueError('报告摘要不一致')
        # A revoked source also revokes access to frozen prose quoting it.
        from enterprise.knowledge import Repository
        knowledge = Repository(self.root, principal=actor)
        for source in payload.get('sources', []):
            if source.get('version_id') and not knowledge.get(version_id=source['version_id']):
                raise PermissionError('报告包含当前无权读取的知识证据')
        return row, payload

    def _view(self, row, payload=None):
        result = {k: row[k] for k in row.keys() if k != 'payload'}
        result['factories'] = json.loads(result['factories'])
        result['report_id'] = result['id']
        result['review_status'] = result['status']
        if payload is not None:
            result['payload'] = payload
        return result

    @guarded_write
    def save(self, payload, *, actor):
        from report.model import verify_payload, canonical
        verify_payload(payload)
        product = payload['params']['product']
        factories = ['中药一厂']
        benchmark = payload.get('benchmark') or {}
        if payload['params'].get('include_benchmark') or benchmark.get('periods'):
            factories.append('中药二厂')
        for factory in factories:
            require(actor, 'report.generate', product=product, factory=factory)
        ident = payload['report_id']
        with closing(self._connect()) as con, con:
            previous = con.execute('SELECT * FROM reports WHERE id=?', (ident,)).fetchone()
            if previous:
                previous, saved_payload = self._load(con, ident, actor)
                if previous['hash'] != payload['frozen_hash']:
                    raise ValueError('相同报告ID不能覆盖不同内容')
                return self._view(previous, saved_payload)
            created = now()
            con.execute('INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (ident, actor.tenant_id, product, json.dumps(factories, ensure_ascii=False),
                         actor.user_id, created, payload['frozen_hash'], canonical(payload), 'draft', 1, None, None, ''))
            con.execute('INSERT INTO report_events(report_id,ts,action,actor,reason,version) VALUES(?,?,?,?,?,?)',
                        (ident, created, 'generated', actor.user_id, payload.get('generation_status', ''), 1))
        return self.get(ident, actor=actor)

    def get(self, ident, *, actor):
        with closing(self._connect()) as con:
            row, payload = self._load(con, ident, actor)
            return self._view(row, payload)

    def list(self, *, actor):
        require(actor, 'report.read')
        if not self.db.exists():
            return []
        with closing(self._connect()) as con:
            rows = con.execute('SELECT * FROM reports ORDER BY created DESC').fetchall()
        result = []
        for row in rows:
            try:
                self._authorize(row, actor)
                result.append(self._view(row))
            except PermissionError:
                continue
        return result

    @guarded_write
    def _transition(self, ident, actor, expected_version, target, reason):
        action = 'report.approve' if target in ('approved', 'rejected') else 'report.generate'
        with closing(self._connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row, payload = self._load(con, ident, actor, action)
            if row['version'] != expected_version:
                raise ValueError('报告审批状态已变化，请刷新')
            allowed = {'submitted': ('draft', 'rejected'), 'approved': ('submitted',), 'rejected': ('submitted',)}
            if row['status'] not in allowed[target]:
                raise ValueError('当前状态不能执行此审批动作')
            if target in ('approved', 'rejected'):
                if not reason.strip():
                    raise ValueError('审核意见必填')
                if actor.auth_method != 'local_os_demo' and row['creator'] == actor.user_id:
                    raise PermissionError('企业模式由另一位主管审核，报告创建人不可自批')
            if target == 'approved' and not payload.get('formal'):
                raise ValueError('存在资料缺口的核查稿不能签发为正式报告')
            timestamp = now()
            approver = actor.user_id if target == 'approved' else None
            con.execute('UPDATE reports SET status=?,version=version+1,approver=?,approved=?,reason=? WHERE id=?',
                        (target, approver, timestamp if approver else None, reason, ident))
            con.execute('INSERT INTO report_events(report_id,ts,action,actor,reason,version) VALUES(?,?,?,?,?,?)',
                        (ident, timestamp, target, actor.user_id, reason, expected_version+1))
        return self.get(ident, actor=actor)

    def submit(self, ident, *, actor, expected_version):
        return self._transition(ident, actor, expected_version, 'submitted', '提交专业复核')

    def approve(self, ident, *, actor, expected_version, reason):
        return self._transition(ident, actor, expected_version, 'approved', reason)

    def reject(self, ident, *, actor, expected_version, reason):
        return self._transition(ident, actor, expected_version, 'rejected', reason)

    def events(self, ident, *, actor):
        with closing(self._connect()) as con:
            self._load(con, ident, actor)
            return [dict(row) for row in con.execute('SELECT * FROM report_events WHERE report_id=? ORDER BY id', (ident,))]

    @guarded_write
    def export(self, ident, format='docx', *, actor):
        if format not in ('docx', 'pdf', 'audit_json'):
            raise ValueError('导出格式须为docx、pdf或audit_json')
        with closing(self._connect()) as con, con:
            row, payload = self._load(con, ident, actor)
            artifact = con.execute('SELECT * FROM report_artifacts WHERE report_id=? AND format=? AND version=?',
                                   (ident, format, row['version'])).fetchone()
            if artifact:
                if hashlib.sha256(artifact['content']).hexdigest() != artifact['hash']:
                    raise ValueError('已归档报告文件摘要不一致')
                return artifact['content']
            exported = frozen_export_payload(payload, row)
            from enterprise.report_service import export_report
            content = export_report(exported, format)
            con.execute('INSERT INTO report_artifacts VALUES(?,?,?,?,?,?)',
                        (ident, format, row['version'], hashlib.sha256(content).hexdigest(), content, now()))
            return content

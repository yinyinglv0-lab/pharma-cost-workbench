"""Authorized period closing/reopening with append-only decision events."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from paths import MANAGED_DIR
from enterprise.security import require


class PeriodRepository:
    def __init__(self, root=None):
        self.root = Path(root) if root else MANAGED_DIR
        self.db = self.root / 'periods.db'

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db, timeout=15)
        con.row_factory = sqlite3.Row
        con.executescript('''
            CREATE TABLE IF NOT EXISTS periods(factory TEXT, product TEXT, month TEXT,
                state TEXT NOT NULL, version INTEGER NOT NULL, actor TEXT NOT NULL,
                reason TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(factory,product,month));
            CREATE TABLE IF NOT EXISTS period_events(id INTEGER PRIMARY KEY, factory TEXT,
                product TEXT, month TEXT, state TEXT, version INTEGER, actor TEXT,
                reason TEXT, updated TEXT);
            CREATE TRIGGER IF NOT EXISTS period_events_no_update BEFORE UPDATE ON period_events
                BEGIN SELECT RAISE(ABORT,'period events are append only'); END;
            CREATE TRIGGER IF NOT EXISTS period_events_no_delete BEFORE DELETE ON period_events
                BEGIN SELECT RAISE(ABORT,'period events are append only'); END;
        ''')
        return con

    def set_state(self, factory, product, month, state, *, actor, reason, expected_version=0):
        require(actor, 'period.manage', factory=factory, product=product)
        if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month) or state not in ('closed', 'open'):
            raise ValueError('期间或状态无效')
        if not reason or not reason.strip():
            raise ValueError('关账或重开必须填写原因')
        from enterprise.operations import write_guard
        with write_guard(self.root), closing(self._connect()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            old = con.execute('SELECT * FROM periods WHERE factory=? AND product=? AND month=?',
                              (factory, product, month)).fetchone()
            version = old['version'] if old else 0
            if version != expected_version:
                raise ValueError('期间状态已被其他用户修改，请刷新')
            now = datetime.now(timezone.utc).isoformat()
            values = (factory, product, month, state, version + 1, actor.user_id, reason.strip(), now)
            con.execute('INSERT INTO periods VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(factory,product,month) '
                        'DO UPDATE SET state=excluded.state,version=excluded.version,actor=excluded.actor,'
                        'reason=excluded.reason,updated=excluded.updated', values)
            con.execute('INSERT INTO period_events(factory,product,month,state,version,actor,reason,updated) '
                        'VALUES(?,?,?,?,?,?,?,?)', values)
        return dict(zip(('factory','product','month','state','version','actor','reason','updated'), values))

    def list(self, *, actor):
        require(actor, 'data.read')
        if not self.db.exists():
            return []
        with closing(self._connect()) as con:
            rows = [dict(row) for row in con.execute('SELECT * FROM periods ORDER BY month DESC,factory,product')]
        from enterprise.security import can
        return [row for row in rows if can(actor, 'data.read', factory=row['factory'], product=row['product'])]

    def assert_open(self, changes):
        if not self.db.exists():
            return
        with closing(self._connect()) as con:
            for change in changes:
                key = change['key']
                row = con.execute('SELECT state FROM periods WHERE factory=? AND product=? AND month=?',
                                  (key['工厂'],key['产品名称'],key['月份'])).fetchone()
                if row and row['state'] == 'closed':
                    raise ValueError(f"{key['工厂']} {key['产品名称']} {key['月份']}已关账；须先授权重开，后续报告应标记重述")

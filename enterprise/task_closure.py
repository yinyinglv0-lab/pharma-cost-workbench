"""Business acceptance and durable reminders; imported without I/O.

Remote execution, human acceptance, local acknowledgement and mock notification
receipts are independent facts. No remote completed status can close a task.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from uuid import uuid4

from enterprise.operations import assert_dispatch_allowed, guarded_write
from enterprise.rpa_client import RPAError
from enterprise.security import require

BUSINESS_TZ = timezone(timedelta(hours=8))
CLOSURE_COLUMNS = {
    "business_status": "TEXT NOT NULL DEFAULT 'open'",
    "closure_revision": "INTEGER NOT NULL DEFAULT 0",
    "execution_completed_utc": "TEXT",
    "closed_utc": "TEXT",
}
CLOSURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_rectifications (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
 version INTEGER NOT NULL, closure_revision INTEGER NOT NULL,
 created_utc TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL,
 payload_hash TEXT NOT NULL, UNIQUE(task_id,closure_revision)
);
CREATE TABLE IF NOT EXISTS task_acceptance_reviews (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
 submission_id TEXT NOT NULL REFERENCES task_rectifications(id),
 closure_revision INTEGER NOT NULL, created_utc TEXT NOT NULL,
 actor TEXT NOT NULL, decision TEXT NOT NULL, comment TEXT NOT NULL,
 UNIQUE(task_id,closure_revision)
);
CREATE TABLE IF NOT EXISTS task_notifications (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id),
 version INTEGER NOT NULL, sequence INTEGER NOT NULL,
 idempotency_key TEXT NOT NULL UNIQUE, created_utc TEXT NOT NULL,
 created_at REAL NOT NULL, channel TEXT NOT NULL DEFAULT 'wechat_mock',
 escalation TEXT NOT NULL, payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
 external_status TEXT NOT NULL DEFAULT 'not_sent',
 delivery_status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at REAL, lease_token TEXT, lease_until REAL, receipt TEXT, last_error TEXT,
 UNIQUE(task_id,version,sequence)
);
CREATE INDEX IF NOT EXISTS task_notifications_due ON task_notifications(delivery_status,next_attempt_at);
CREATE TABLE IF NOT EXISTS task_notification_reads (
 notification_id TEXT NOT NULL REFERENCES task_notifications(id), user_id TEXT NOT NULL,
 read_utc TEXT NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(notification_id,user_id)
);
"""


def migrate_closure(con):
    """Add columns only. Historical execution timestamps are never acceptance."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
    for name, definition in CLOSURE_COLUMNS.items():
        if name not in existing:
            con.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            if name == "execution_completed_utc":
                con.execute("UPDATE tasks SET execution_completed_utc=completed_utc WHERE receipt_status='completed'")
    con.executescript(CLOSURE_SCHEMA)
    for table in ("task_rectifications", "task_acceptance_reviews", "task_notification_reads"):
        for operation in ("UPDATE", "DELETE"):
            con.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'business audit is append only'); END")


def business_today(clock):
    return datetime.fromtimestamp(clock(), BUSINESS_TZ).date().isoformat()


def overdue_sql(alias="t"):
    return (f"({alias}.workflow_status='issued' AND {alias}.business_status<>'closed' "
            f"AND COALESCE(json_extract({alias}.content,'$.deadline'),'')<>'' "
            f"AND json_extract({alias}.content,'$.deadline')<?)")


def closure_view(row, content, clock):
    deadline = content.get("deadline") or ""
    overdue = bool(row["workflow_status"] == "issued" and row["business_status"] != "closed"
                   and deadline and deadline < business_today(clock))
    return {"is_overdue": overdue, "deadline_status": "overdue" if overdue else
            "closed" if row["business_status"] == "closed" else "not_due" if deadline else "unspecified",
            "deadline_timezone": "UTC+08:00"}


def _payload(payload):
    from enterprise.task_workflow import TaskError, _text, _strings
    keys = {"summary", "evidence", "metrics", "evaluation"}
    if not isinstance(payload, dict) or set(payload) != keys:
        raise TaskError("整改提交须包含summary/evidence/metrics/evaluation，不允许指定验收人、时间或状态")
    result = {key: _text(payload[key], key, max_length=8000) for key in ("summary", "evaluation")}
    result["evidence"], result["metrics"] = [], []
    for key in ("evidence", "metrics"):
        if not isinstance(payload[key], list) or len(payload[key]) > 30:
            raise TaskError(f"{key}须为不超过30项的列表")
    for item in payload["evidence"]:
        if not isinstance(item, dict) or set(item) != {"name", "reference", "sha256"}:
            raise TaskError("每份凭证须包含name/reference/sha256")
        evidence = {key: _text(item[key], f"evidence.{key}", max_length=1000) for key in item}
        if evidence["sha256"] and not re.fullmatch(r"[0-9a-fA-F]{64}", evidence["sha256"]):
            raise TaskError("凭证sha256须为原件64位十六进制哈希；缺失时留空待补充")
        evidence["sha256"] = evidence["sha256"].lower()
        result["evidence"].append(evidence)
    names = [item["name"] for item in result["evidence"]]
    if len(names) != len(set(names)):
        raise TaskError("凭证名称须唯一，供前后指标引用")
    metric_keys = {"name", "before", "after", "unit", "before_period", "after_period", "scope", "method", "evidence_ids"}
    for item in payload["metrics"]:
        if not isinstance(item, dict) or set(item) != metric_keys:
            raise TaskError("效果指标须包含名称、前后值、单位、前后期间、范围、方法和凭证引用")
        metric = {key: _text(item[key], f"metrics.{key}", max_length=2000)
                  for key in metric_keys - {"before", "after", "evidence_ids"}}
        for key in ("before", "after"):
            value = item[key]
            if value is None or value == "":
                metric[key] = None
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 100:
                raise TaskError("前后指标须为有限数值或null，不得把缺失值填为0")
            try:
                number = Decimal(str(value))
                if not number.is_finite() or abs(number) > Decimal("1e30") or not -12 <= number.as_tuple().exponent <= 30:
                    raise InvalidOperation
            except InvalidOperation:
                raise TaskError("前后指标须为有限数值或null") from None
            metric[key] = format(number, "f")
        metric["evidence_ids"] = _strings(item["evidence_ids"], "metrics.evidence_ids")
        if not set(metric["evidence_ids"]).issubset(names):
            raise TaskError("效果指标只能引用本次提交的凭证名称")
        result["metrics"].append(metric)
    from enterprise.task_workflow import canonical
    if len(canonical(result)) > 100_000:
        raise TaskError("整改材料超过保存上限")
    return result


def missing_acceptance_material(payload):
    """Incomplete submissions remain reviewable, never qualify for closed."""
    missing = []
    placeholder = lambda value: value in (None, "", "待补充", "待指定")
    for key, label in (("summary", "整改结果"), ("evaluation", "效果评估")):
        if placeholder(payload[key]):
            missing.append(label)
    if not payload["evidence"] or any(any(placeholder(v) for v in item.values()) for item in payload["evidence"]):
        missing.append("原始凭证名称、定位及原件哈希")
    if not payload["metrics"] or any(any(placeholder(v) for k, v in item.items() if k != "evidence_ids")
                                      or not item["evidence_ids"] for item in payload["metrics"]):
        missing.append("同口径前后指标、比较期间、范围、方法及凭证引用")
    return missing


class TaskClosureMixin:
    def _closure_expected(self, row, expected_version, expected_closure_revision):
        from enterprise.task_workflow import TaskConflict
        self._expected(row, expected_version)
        if type(expected_closure_revision) is not int or row["closure_revision"] != expected_closure_revision:
            raise TaskConflict("整改材料或验收版本已变化，请重新读取后操作")

    def submit_rectification(self, task_id, payload, *, actor, expected_version, expected_closure_revision):
        from enterprise.task_workflow import TaskConflict, canonical, _actor, _hash, _utc
        require(actor, "task.rectify")
        payload = _payload(payload)
        with self._transaction() as con:
            row = self._load(con, task_id, actor, "task.rectify")
            self._closure_expected(row, expected_version, expected_closure_revision)
            if row["workflow_status"] != "issued" or row["business_status"] not in {"open", "rework"}:
                raise TaskConflict("仅已签发且待整改或返工中的任务可提交材料")
            revision, now, ident = row["closure_revision"] + 1, _utc(self.clock()), uuid4().hex
            con.execute("INSERT INTO task_rectifications VALUES(?,?,?,?,?,?,?,?)",
                        (ident, task_id, row["version"], revision, now, canonical(_actor(actor)), canonical(payload), _hash(payload)))
            con.execute("UPDATE tasks SET business_status='pending_acceptance',closure_revision=?,updated_utc=? WHERE task_id=?",
                        (revision, now, task_id))
            self._event(con, row, "rectification_submitted", actor,
                        {"submission_id": ident, "closure_revision": revision, "payload_hash": _hash(payload),
                         "missing_material": missing_acceptance_material(payload)})
            return self._view(con, self._load(con, task_id, actor))

    def review_rectification(self, task_id, *, decision, comment, actor, expected_version, expected_closure_revision):
        from enterprise.task_workflow import TaskError, TaskConflict, canonical, _actor, _text, _utc
        require(actor, "task.accept")
        if decision not in {"accept", "rework"}:
            raise TaskError("验收decision须为accept或rework")
        comment = _text(comment, "验收意见", required=True, max_length=4000)
        with self._transaction() as con:
            row = self._load(con, task_id, actor, "task.accept")
            self._closure_expected(row, expected_version, expected_closure_revision)
            if row["business_status"] != "pending_acceptance":
                raise TaskConflict("仅待验收材料可验收或退回返工")
            submission = con.execute("SELECT * FROM task_rectifications WHERE task_id=? ORDER BY closure_revision DESC LIMIT 1", (task_id,)).fetchone()
            content = json.loads(row["content"])
            version_author = con.execute("SELECT actor FROM task_versions WHERE task_id=? AND version=?", (task_id, row["version"])).fetchone()
            authors = {json.loads(submission["actor"])["user_id"], json.loads(row["created_by"])["user_id"]}
            if version_author:
                authors.add(json.loads(version_author[0])["user_id"])
            if actor.user_id in authors or content["assignee"].get("name") in {actor.display_name, actor.user_id}:
                raise PermissionError("验收须由独立人员完成，任务作者、责任人和本次材料提交人不可自验；本机演示同样适用")
            payload = json.loads(submission["payload"])
            if decision == "accept":
                missing = missing_acceptance_material(payload)
                if missing:
                    raise TaskError("验收材料待补充：" + "、".join(missing))
                if row["receipt_status"] != "completed":
                    raise TaskConflict("远端执行尚未完成，不能关闭业务任务")
            revision, now = row["closure_revision"] + 1, _utc(self.clock())
            con.execute("INSERT INTO task_acceptance_reviews VALUES(?,?,?,?,?,?,?,?)",
                        (uuid4().hex, task_id, submission["id"], revision, now, canonical(_actor(actor)), decision, comment))
            state = "closed" if decision == "accept" else "rework"
            con.execute("UPDATE tasks SET business_status=?,closure_revision=?,closed_utc=?,updated_utc=? WHERE task_id=?",
                        (state, revision, now if decision == "accept" else None, now, task_id))
            if state == "closed":
                con.execute("UPDATE task_notifications SET delivery_status='cancelled',next_attempt_at=NULL WHERE task_id=? AND delivery_status IN ('pending','retry')", (task_id,))
            self._event(con, row, "business_closed" if state == "closed" else "rectification_rework", actor,
                        {"submission_id": submission["id"], "closure_revision": revision, "comment": comment})
            view = self._view(con, self._load(con, task_id, actor))
        if state == "closed":
            # 尽力而为：把闭环事实自动暂存为「异常处理记录」知识候选（仍须知识管理员确认发布）。
            # 自动暂存失败绝不影响业务关闭结果。
            try:
                from enterprise.anomaly_case import stage_closed_task
                view['anomaly_case'] = stage_closed_task(self.root, task_id,
                    json.loads(row["content"]), payload, revision, now, actor)
            except Exception:
                view['anomaly_case'] = {'staged': False, 'reason': 'auto_stage_error', 'title': None,
                                        'doc_id': None, 'stage_id': None}
        return view

    def rectifications(self, task_id, *, actor):
        require(actor, "task.read")
        with self._transaction() as con:
            self._load(con, task_id, actor)
            submissions, reviews = [], []
            for row in con.execute("SELECT * FROM task_rectifications WHERE task_id=? ORDER BY closure_revision", (task_id,)):
                value = dict(row)
                value["actor"], value["payload"] = json.loads(row["actor"]), json.loads(row["payload"])
                value["missing_material"] = missing_acceptance_material(value["payload"])
                submissions.append(value)
            for row in con.execute("SELECT * FROM task_acceptance_reviews WHERE task_id=? ORDER BY closure_revision", (task_id,)):
                reviews.append({**dict(row), "actor": json.loads(row["actor"])})
            return {"submissions": submissions, "reviews": reviews}

    @staticmethod
    def _notification_view(con, row, actor):
        value = dict(row)
        for key in ("lease_token", "lease_until"):
            value.pop(key, None)
        for key in ("payload", "receipt", "last_error"):
            value[key] = json.loads(value[key]) if value[key] else None
        read = con.execute("SELECT read_utc FROM task_notification_reads WHERE notification_id=? AND user_id=?", (row["id"], actor.user_id)).fetchone()
        value["read_utc"] = read[0] if read else None
        value["simulated"] = True
        return value

    def notifications(self, *, actor, task_id=None, unread_only=False, limit=100, offset=0):
        from enterprise.task_workflow import TaskError
        require(actor, "task.read")
        if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or not 0 <= offset <= 2**63-1 or type(unread_only) is not bool:
            raise TaskError("通知分页参数无效")
        where, parameters = self._read_filter(actor, None, None)
        if task_id is not None:
            where += " AND t.task_id=?"
            parameters.append(task_id)
        if unread_only:
            where += " AND NOT EXISTS(SELECT 1 FROM task_notification_reads r WHERE r.notification_id=n.id AND r.user_id=?)"
            parameters.append(actor.user_id)
        with self._transaction() as con:
            if task_id is not None:
                self._load(con, task_id, actor)
            rows = con.execute("SELECT n.* FROM task_notifications n JOIN tasks t ON t.task_id=n.task_id WHERE " + where +
                               " ORDER BY n.created_at DESC,n.id LIMIT ? OFFSET ?", [*parameters, limit, offset])
            return [self._notification_view(con, row, actor) for row in rows]

    def acknowledge_notification(self, notification_id, *, actor):
        from enterprise.task_workflow import TaskNotFound, canonical, _actor, _utc
        require(actor, "task.read")
        with self._transaction() as con:
            note = con.execute("SELECT * FROM task_notifications WHERE id=?", (notification_id,)).fetchone()
            if note is None:
                raise TaskNotFound("通知不存在")
            row = self._load(con, note["task_id"], actor)
            inserted = con.execute("INSERT OR IGNORE INTO task_notification_reads VALUES(?,?,?,?)",
                                   (notification_id, actor.user_id, _utc(self.clock()), canonical(_actor(actor))))
            if inserted.rowcount:
                self._event(con, row, "reminder_read", actor, {"notification_id": notification_id})
            return self._notification_view(con, note, actor)

    def schedule_reminders(self, *, actor, task_id=None, limit=100):
        from enterprise.task_workflow import TaskError, _scope, canonical, _hash, _utc
        require(actor, "task.remind")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise TaskError("催办批次须为1–100")
        now, result = self.clock(), []
        where, parameters = self._read_filter(actor, None, None)
        where += " AND " + overdue_sql()
        parameters.append(business_today(self.clock))
        if task_id is not None:
            where += " AND t.task_id=?"
            parameters.append(task_id)
        # Pending/unknown delivery blocks later reminders; don't evade ambiguity by
        # assigning a fresh key. Failed definite attempts may be reminded next day.
        where += (" AND NOT EXISTS(SELECT 1 FROM task_notifications n WHERE n.task_id=t.task_id AND "
                  "(n.delivery_status IN ('pending','retry','sending','unknown') OR n.created_at>?))")
        parameters.append(now - self.reminder_interval_seconds)
        with self._transaction() as con:
            if task_id is not None:
                self._load(con, task_id, actor, "task.remind")
            rows = con.execute("SELECT t.* FROM tasks t WHERE " + where + " ORDER BY json_extract(t.content,'$.deadline'),t.task_id LIMIT ?", [*parameters, limit]).fetchall()
            for row in rows:
                content = json.loads(row["content"])
                try:
                    _scope(actor, "task.remind", content, row["tenant_id"])
                except PermissionError:
                    continue
                sequence = con.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM task_notifications WHERE task_id=? AND version=?", (row["task_id"], row["version"])).fetchone()[0]
                escalation = "supervisor_attention" if sequence >= 3 else "assignee"
                message = (f"任务“{content['task_title']}”已超过{content['deadline']}业务截止日，尚未完成整改验收。"
                           "请补充整改证据与同口径前后指标，或说明返工进度。" + ("已连续三次以上催办，请主管关注。" if sequence >= 3 else ""))
                payload = {"recipient": content["assignee"]["name"], "department": content["assignee"]["department"], "message": message}
                ident = uuid4().hex
                con.execute("INSERT INTO task_notifications(id,task_id,version,sequence,idempotency_key,created_utc,created_at,escalation,payload,payload_hash,next_attempt_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (ident, row["task_id"], row["version"], sequence, f"{row['tenant_id']}:{row['task_id']}:v{row['version']}:reminder:{sequence}",
                             _utc(now), now, escalation, canonical(payload), _hash(payload), now))
                self._event(con, row, "reminder_scheduled", actor, {"notification_id": ident, "sequence": sequence, "external_status": "not_sent", "escalation": escalation})
                result.append(self._notification_view(con, con.execute("SELECT * FROM task_notifications WHERE id=?", (ident,)).fetchone(), actor))
        return result

    @guarded_write
    def dispatch_reminders(self, client, *, actor, task_id=None, limit=10):
        from enterprise.task_workflow import TaskError, TaskConflict, _scope, canonical, _hash
        require(actor, "task.remind")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise TaskError("催办派发批次须为1–100")
        assert_dispatch_allowed(self.root)
        client.ensure_enabled()
        if self.lease_seconds < client.config.timeout_seconds * 4:
            raise TaskError("催办租约须覆盖至少四倍单次HTTP超时")
        result, seen = [], set()
        for _ in range(limit):
            with self._transaction() as con:
                where, parameters = self._read_filter(actor, None, None)
                if task_id is not None:
                    self._load(con, task_id, actor, 'task.remind')
                    where += ' AND t.task_id=?'
                    parameters.append(task_id)
                notes = con.execute("SELECT n.* FROM task_notifications n JOIN tasks t ON t.task_id=n.task_id WHERE " + where +
                                    " AND (n.delivery_status IN ('pending','retry') AND n.next_attempt_at<=? OR n.delivery_status='sending' AND n.lease_until<=?) ORDER BY n.created_at,n.id",
                                    [*parameters, self.clock(), self.clock()]).fetchall()
                chosen = None
                for note in notes:
                    if note["id"] in seen:
                        continue
                    row = self._load(con, note["task_id"], actor, "task.remind")
                    seen.add(note["id"])
                    if note["delivery_status"] == "sending":
                        con.execute("UPDATE task_notifications SET delivery_status='unknown',external_status='unknown',lease_token=NULL,lease_until=NULL,next_attempt_at=NULL WHERE id=?", (note["id"],))
                        self._event(con, row, "reminder_unknown", actor, {"notification_id": note["id"], "reason": "发送进程中断，官方通知接口无幂等查询，禁止重发"})
                        continue
                    if row["business_status"] == "closed":
                        con.execute("UPDATE task_notifications SET delivery_status='cancelled',next_attempt_at=NULL WHERE id=?", (note["id"],))
                        continue
                    payload = json.loads(note["payload"])
                    if _hash(payload) != note["payload_hash"]:
                        raise TaskConflict("催办内容哈希不一致")
                    # Refresh current worker and original issuer, as for task POST.
                    for subject in (actor.user_id, json.loads(row["issued_by"])["user_id"]):
                        current = self._resolve_current(subject)
                        if current.user_id != subject:
                            raise PermissionError("催办身份解析不一致")
                        _scope(current, "task.remind", json.loads(row["content"]), row["tenant_id"])
                    token = uuid4().hex
                    con.execute("UPDATE task_notifications SET delivery_status='sending',attempts=attempts+1,lease_token=?,lease_until=? WHERE id=?", (token, self.clock()+self.lease_seconds, note["id"]))
                    chosen = (dict(note), payload, token)
                    break
                if chosen is None:
                    break
            note, payload, token = chosen
            receipt, error = None, None
            try:
                receipt = client.notify_wechat(payload)
            except RPAError as exc:
                error = exc
            # An unclassified crash deliberately leaves 'sending'; lease recovery
            # above marks unknown, never repeats a potentially delivered POST.
            with self._transaction() as con:
                row = self._load(con, note["task_id"], actor, "task.remind")
                active = con.execute("SELECT * FROM task_notifications WHERE id=? AND lease_token=?", (note["id"], token)).fetchone()
                if active is None:
                    raise TaskConflict("催办租约已变化，不能覆盖后续结果")
                if error:
                    status = "unknown" if error.ambiguous else "retry" if error.retryable and active["attempts"] < self.max_attempts else "failed"
                    external = "unknown" if error.ambiguous else "not_sent"
                    next_at = self.clock() + max(30.0, self.retry_base_seconds * 2**active["attempts"]) if status == "retry" else None
                else:
                    status, external, next_at = "delivered", "delivered", None
                con.execute("UPDATE task_notifications SET delivery_status=?,external_status=?,next_attempt_at=?,lease_token=NULL,lease_until=NULL,receipt=?,last_error=? WHERE id=?",
                            (status, external, next_at, canonical(receipt) if receipt else None, canonical(error.as_dict()) if error else None, note["id"]))
                self._event(con, row, "reminder_delivered" if receipt else "reminder_delivery_failed", actor,
                            {"notification_id": note["id"], "external_status": external, "simulated": True, "error": error.as_dict() if error else None})
                result.append(self._notification_view(con, con.execute("SELECT * FROM task_notifications WHERE id=?", (note["id"],)).fetchone(), actor))
        return result

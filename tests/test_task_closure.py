"""Business closure contracts. Synthetic principals, isolated DBs, no live services."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import httpx
import pytest

from enterprise.rpa_client import RPAClient, RPAConfig, RPAPolicyError
from enterprise.security import Principal
from enterprise.task_workflow import TaskRepository, TaskConflict, TaskError, SCHEMA, canonical, _normalise
from enterprise.task_plan import default_plan

AUTHOR = Principal('closure-author', '测试材料提交员', ('analyst',), ('测试一厂',), ('测试产品',))
REVIEWER = Principal('closure-reviewer', '测试独立验收员', ('supervisor',), ('测试一厂',), ('测试产品',))
AUDITOR = Principal('closure-auditor', '测试审计员', ('auditor',), ('测试一厂',), ('测试产品',))


def draft():
    return {'task_title': '测试产品成本核查', 'assignee': {'name': AUTHOR.display_name, 'department': '测试成本部', 'role': ''},
            'source': {'analysis_type': '季度成本分析', 'analysis_month': '2026-06', 'product': '测试产品', 'finding': '成本归集存在差异，实际原因待核查。'},
            'deadline': '2026-09-20', 'priority': 'medium', 'suggestion': '', 'factories': ['测试一厂'],
            'evidence_ids': ['TEST-F01'], 'evidence_hashes': {'TEST-F01': hashlib.sha256(b'synthetic source').hexdigest()}}


def material(before='12.5', after='12.6'):
    return {'summary': '隔离测试已复核费用归集。', 'evaluation': '隔离测试费用增加0.1元/盒，没有实现节约。',
            'evidence': [{'name': 'TEST原始凭证', 'reference': '测试归档/record-01.csv', 'sha256': hashlib.sha256(b'synthetic comparison fixture').hexdigest()}],
            'metrics': [{'name': '单位费用', 'before': before, 'after': after, 'unit': '元/盒',
                         'before_period': '2026-05', 'after_period': '2026-06', 'scope': '测试一厂/测试产品/同规格',
                         'method': '相同产品规格、费用范围，费用除以对应产量', 'evidence_ids': ['TEST原始凭证']}]}


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc).timestamp()
    def __call__(self):
        return self.value


class Remote:
    def __init__(self):
        self.tasks, self.notifications, self.calls = {}, [], []
        self.fault = None
    def handler(self, request):
        self.calls.append((request.method, request.url.path))
        if request.url.path == '/api/notify/wechat':
            if self.fault:
                return self.fault(request)
            value = json.loads(request.content)
            self.notifications.append(value)
            return httpx.Response(200, json={'code': 200, 'data': {'status': 'delivered', 'recipient': value['recipient'], 'message_id': f'TEST-{len(self.notifications)}', 'sent_at': '2026-09-21T00:00:00'}})
        if request.method == 'POST':
            value = json.loads(request.content)
            self.tasks[value['task_id']] = {**value, 'status': 'sent'}
            return httpx.Response(200, json={'code': 200, 'data': self.tasks[value['task_id']]})
        value = self.tasks.get(request.url.path.rsplit('/', 1)[-1])
        return httpx.Response(200, json={'code': 200, 'data': value}) if value else httpx.Response(404)
    def client(self):
        return RPAClient(RPAConfig(timeout_seconds=.1), transport=httpx.MockTransport(self.handler))


@pytest.fixture
def setup(tmp_path):
    clock, remote = Clock(), Remote()
    principals = {actor.user_id: actor for actor in (AUTHOR, REVIEWER, AUDITOR)}
    repo = TaskRepository(tmp_path / 'closure', clock=clock, lease_seconds=2, principal_resolver=lambda ident: principals[ident])
    task = repo.create(draft(), actor=AUTHOR, task_id='TASK-CLOSURE-TEST')
    repo.submit(task['task_id'], actor=AUTHOR, expected_version=1)
    repo.approve(task['task_id'], actor=REVIEWER, expected_version=1)
    repo.enqueue(task['task_id'], actor=REVIEWER, expected_version=1)
    return repo, clock, remote, task['task_id']


def finish_execution(repo, remote, ident):
    with remote.client() as client:
        repo.dispatch(client, actor=REVIEWER)
        remote.tasks[ident]['status'] = 'completed'
        return repo.sync(ident, client, actor=REVIEWER)


def test_receipt_preserves_official_notify_status_for_frontend_display(tmp_path):
    """官方 mock 创建任务回执的 notify_status.wechat（已发送至XX责任人）须随回执保留。"""
    from enterprise.task_workflow import TaskRepository
    class NotifyRemote:
        def __init__(self):
            self.tasks = {}
        def handler(self, request):
            if request.method == 'POST' and request.url.path == '/api/rpa/tasks':
                value = json.loads(request.content)
                self.tasks[value['task_id']] = {**value, 'status': 'sent',
                    'notify_status': {'wechat': f"已发送至 {value['assignee']['name']}({value['assignee']['department']})",
                                      'sent_at': '2026-09-20T16:00:01'}}
                return httpx.Response(200, json={'code': 200, 'data': self.tasks[value['task_id']]})
            value = self.tasks.get(request.url.path.rsplit('/', 1)[-1])
            return httpx.Response(200, json={'code': 200, 'data': value}) if value else httpx.Response(404)
        def client(self):
            return RPAClient(RPAConfig(timeout_seconds=.1), transport=httpx.MockTransport(self.handler))
    clock, remote = Clock(), NotifyRemote()
    principals = {actor.user_id: actor for actor in (AUTHOR, REVIEWER)}
    repo = TaskRepository(tmp_path / 'closure', clock=clock, lease_seconds=2,
                          principal_resolver=lambda ident: principals[ident])
    task = repo.create(draft(), actor=AUTHOR, task_id='TASK-NOTIFY')
    repo.submit(task['task_id'], actor=AUTHOR, expected_version=1)
    repo.approve(task['task_id'], actor=REVIEWER, expected_version=1)
    repo.enqueue(task['task_id'], actor=REVIEWER, expected_version=1)
    with remote.client() as client:
        repo.dispatch(client, actor=REVIEWER)
    view = repo.get(task['task_id'], actor=REVIEWER)
    assert view['receipt_status'] == 'sent'
    notify = view['receipt']['notify_status']
    assert notify['wechat'] == '已发送至 测试材料提交员(测试成本部)'
    assert notify['sent_at'] == '2026-09-20T16:00:01'


def test_deadline_timezone_independent_of_remote_and_keeps_unclosed_execution_overdue(setup):
    repo, clock, remote, ident = setup
    clock.value -= .001
    assert not repo.get(ident, actor=AUTHOR)['is_overdue']
    clock.value += .001
    assert repo.get(ident, actor=AUTHOR)['is_overdue']
    value = finish_execution(repo, remote, ident)
    assert value['execution_completed_utc'] == value['completed_utc']
    assert value['business_status'] == 'open' and value['closed_utc'] is None and value['is_overdue']
    assert repo.summary(actor=AUTHOR)['execution_completed'] == 1
    assert repo.summary(actor=AUTHOR)['completed'] == 0
    assert repo.summary(actor=AUTHOR)['business_overdue'] == 1
    assert repo.page(actor=AUTHOR, status='business_overdue')['total'] == 1


def test_rework_preserves_history_then_independent_acceptance_with_cost_increase(setup):
    repo, clock, remote, ident = setup
    finish_execution(repo, remote, ident)
    first = repo.submit_rectification(ident, material(), actor=AUTHOR, expected_version=1, expected_closure_revision=0)
    assert first['business_status'] == 'pending_acceptance' and first['closure_revision'] == 1
    returned = repo.review_rectification(ident, decision='rework', comment='测试：补充范围核对', actor=REVIEWER, expected_version=1, expected_closure_revision=1)
    assert returned['business_status'] == 'rework'
    repo.submit_rectification(ident, material(), actor=AUTHOR, expected_version=1, expected_closure_revision=2)
    closed = repo.review_rectification(ident, decision='accept', comment='测试：范围与原件已核对，成本增加如实保留', actor=REVIEWER, expected_version=1, expected_closure_revision=3)
    assert closed['business_status'] == 'closed' and closed['closure_revision'] == 4 and closed['closed_utc']
    assert not closed['is_overdue']
    history = repo.rectifications(ident, actor=AUDITOR)
    assert len(history['submissions']) == len(history['reviews']) == 2
    assert history['reviews'][-1]['actor']['user_id'] == REVIEWER.user_id
    assert repo.summary(actor=AUTHOR)['closed'] == repo.summary(actor=AUTHOR)['completed'] == 1
    for table in ('task_rectifications', 'task_acceptance_reviews'):
        with sqlite3.connect(repo.db) as con, pytest.raises(sqlite3.IntegrityError):
            con.execute(f'DELETE FROM {table}')


@pytest.mark.parametrize('method', ['oidc', 'local_os_demo'])
def test_self_acceptance_denied_even_in_local_demo(setup, method):
    repo, clock, remote, ident = setup
    finish_execution(repo, remote, ident)
    owner = replace(AUTHOR, roles=('supervisor',), auth_method=method)
    repo.submit_rectification(ident, material(), actor=owner, expected_version=1, expected_closure_revision=0)
    with pytest.raises(PermissionError, match='独立'):
        repo.review_rectification(ident, decision='accept', comment='测试自验', actor=owner, expected_version=1, expected_closure_revision=1)


def test_completion_requires_remote_execution_and_missing_values_never_become_zero(setup):
    repo, clock, remote, ident = setup
    repo.submit_rectification(ident, material(), actor=AUTHOR, expected_version=1, expected_closure_revision=0)
    with pytest.raises(TaskConflict, match='远端执行'):
        repo.review_rectification(ident, decision='accept', comment='测试', actor=REVIEWER, expected_version=1, expected_closure_revision=1)
    repo.review_rectification(ident, decision='rework', comment='测试退回', actor=REVIEWER, expected_version=1, expected_closure_revision=1)
    finish_execution(repo, remote, ident)
    repo.submit_rectification(ident, material(None, None), actor=AUTHOR, expected_version=1, expected_closure_revision=2)
    value = repo.rectifications(ident, actor=AUTHOR)['submissions'][-1]
    assert value['payload']['metrics'][0]['before'] is None and value['missing_material']
    with pytest.raises(TaskError, match='待补充'):
        repo.review_rectification(ident, decision='accept', comment='测试', actor=REVIEWER, expected_version=1, expected_closure_revision=3)


@pytest.mark.parametrize('change', ['fake_reviewer', 'unreferenced', 'hash', 'nan', 'exponent', 'boolean'])
def test_invalid_or_forged_effect_material_rejected(setup, change):
    repo, clock, remote, ident = setup
    value = material()
    if change == 'fake_reviewer':
        value['reviewer'] = '伪造'
    elif change == 'unreferenced':
        value['metrics'][0]['evidence_ids'] = ['不存在']
    elif change == 'hash':
        value['evidence'][0]['sha256'] = 'not-a-hash'
    elif change == 'nan':
        value['metrics'][0]['before'] = 'NaN'
    elif change == 'exponent':
        value['metrics'][0]['before'] = '1e-999999999'
    else:
        value['metrics'][0]['before'] = True
    with pytest.raises(TaskError):
        repo.submit_rectification(ident, value, actor=AUTHOR, expected_version=1, expected_closure_revision=0)


def test_two_concurrent_submissions_and_stale_review_have_single_winner(setup):
    repo, clock, remote, ident = setup
    def submit():
        try:
            return repo.submit_rectification(ident, material(), actor=AUTHOR, expected_version=1, expected_closure_revision=0)
        except TaskConflict:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(value is not None for value in pool.map(lambda _: submit(), range(2))) == 1
    with pytest.raises(TaskConflict):
        repo.review_rectification(ident, decision='rework', comment='过期页面', actor=REVIEWER, expected_version=1, expected_closure_revision=0)
    assert len(repo.rectifications(ident, actor=AUTHOR)['submissions']) == 1


@pytest.mark.parametrize('actor', [AUDITOR, replace(AUTHOR, factories=('其他厂',)), replace(AUTHOR, products=('其他产品',))])
def test_rectification_scope_and_roles_remain_enforced(setup, actor):
    repo, clock, remote, ident = setup
    with pytest.raises(PermissionError):
        repo.submit_rectification(ident, material(), actor=actor, expected_version=1, expected_closure_revision=0)
    with pytest.raises(PermissionError):
        repo.review_rectification(ident, decision='accept', comment='测试', actor=actor, expected_version=1, expected_closure_revision=0)


def test_reminder_concurrency_idempotence_frequency_escalation_and_per_user_reads(setup):
    repo, clock, remote, ident = setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: repo.schedule_reminders(actor=REVIEWER), range(2)))
    assert sum(map(len, results)) == 1
    note = repo.notifications(actor=AUTHOR)[0]
    assert note['external_status'] == 'not_sent'
    with remote.client() as client:
        delivered = repo.dispatch_reminders(client, actor=REVIEWER)[0]
        assert delivered['external_status'] == 'delivered' and delivered['receipt']['message_id']
        assert repo.dispatch_reminders(client, actor=REVIEWER) == []
        clock.value += 86399
        assert repo.schedule_reminders(actor=REVIEWER) == []
        clock.value += 1
        assert repo.schedule_reminders(actor=REVIEWER)[0]['sequence'] == 2
        repo.dispatch_reminders(client, actor=REVIEWER)
        clock.value += 86400
        third = repo.schedule_reminders(actor=REVIEWER)[0]
        assert third['escalation'] == 'supervisor_attention'
        repo.dispatch_reminders(client, actor=REVIEWER)
    assert len(remote.notifications) == 3
    assert not any(path == '/api/rpa/tasks' for _, path in remote.calls)
    first = repo.acknowledge_notification(note['id'], actor=AUTHOR)
    assert first['read_utc'] == repo.acknowledge_notification(note['id'], actor=AUTHOR)['read_utc']
    assert len(repo.notifications(actor=AUTHOR, unread_only=True)) == 2
    assert len(repo.notifications(actor=REVIEWER, unread_only=True)) == 3
    outsider = replace(AUTHOR, factories=('其他厂',))
    assert repo.notifications(actor=outsider) == []
    with pytest.raises(PermissionError):
        repo.acknowledge_notification(note['id'], actor=outsider)


def test_disconnect_never_marks_delivery_and_has_finite_retries(setup):
    repo, clock, remote, ident = setup
    repo.schedule_reminders(actor=REVIEWER)
    def offline(request):
        raise httpx.ConnectError('synthetic offline', request=request)
    remote.fault = offline
    with remote.client() as client:
        for attempt in range(3):
            note = repo.dispatch_reminders(client, actor=REVIEWER)[0]
            assert note['external_status'] == 'not_sent'
            clock.value += 30
        assert note['delivery_status'] == 'failed' and note['attempts'] == 3
        assert repo.dispatch_reminders(client, actor=REVIEWER) == []
    assert not remote.notifications


@pytest.mark.parametrize('fault', ['timeout', '503', 'malformed', 'crash'])
def test_ambiguous_notification_never_reposts_or_uses_new_key(setup, fault):
    repo, clock, remote, ident = setup
    repo.schedule_reminders(actor=REVIEWER)
    def broken(request):
        if fault == 'timeout':
            raise httpx.ReadTimeout('synthetic ambiguous', request=request)
        if fault == 'crash':
            raise RuntimeError('synthetic process crash after possible acceptance')
        return httpx.Response(503 if fault == '503' else 200, json={'code': 200, 'data': {'status': 'banana'}})
    remote.fault = broken
    with remote.client() as client:
        if fault == 'crash':
            with pytest.raises(RuntimeError):
                repo.dispatch_reminders(client, actor=REVIEWER)
        else:
            assert repo.dispatch_reminders(client, actor=REVIEWER)[0]['external_status'] == 'unknown'
        clock.value += 90000
        assert repo.dispatch_reminders(client, actor=REVIEWER) == []
        assert repo.schedule_reminders(actor=REVIEWER) == []
    assert repo.notifications(actor=AUTHOR)[0]['external_status'] == 'unknown'
    assert len(remote.calls) == 1


def test_closed_task_cancels_unsent_reminder_and_does_not_notify(setup):
    repo, clock, remote, ident = setup
    finish_execution(repo, remote, ident)
    repo.schedule_reminders(actor=REVIEWER)
    repo.submit_rectification(ident, material(), actor=AUTHOR, expected_version=1, expected_closure_revision=0)
    repo.review_rectification(ident, decision='accept', comment='测试独立验收', actor=REVIEWER, expected_version=1, expected_closure_revision=1)
    with remote.client() as client:
        assert repo.dispatch_reminders(client, actor=REVIEWER) == []
    assert repo.schedule_reminders(actor=REVIEWER) == []
    assert repo.notifications(actor=AUTHOR)[0]['delivery_status'] == 'cancelled'


def test_legacy_database_migration_does_not_fabricate_closed_or_evidence(tmp_path):
    root = tmp_path / 'legacy'
    root.mkdir()
    content = _normalise(draft())
    with sqlite3.connect(root / 'task_workflow.db') as con:
        con.executescript(SCHEMA)
        con.execute("INSERT INTO tasks(task_id,tenant_id,version,content,content_hash,generation,workflow_status,receipt_status,created_utc,updated_utc,created_by,completed_utc) VALUES(?,?,?,?,?,?,'issued','completed',?,?,?,?)",
                    ('TASK-LEGACY', 'default', 1, canonical(content), 'legacy-hash', '{}', '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', canonical({'user_id': AUTHOR.user_id}), '2026-08-01T00:00:00+00:00'))
    repo = TaskRepository(root)
    value = repo.get('TASK-LEGACY', actor=AUTHOR)
    assert value['execution_completed_utc'] == value['completed_utc']
    assert value['business_status'] == 'open' and value['closed_utc'] is None and value['closure_revision'] == 0
    assert value['content_hash'] == 'legacy-hash'
    assert repo.rectifications('TASK-LEGACY', actor=AUTHOR) == {'submissions': [], 'reviews': []}
    assert repo.get('TASK-LEGACY', actor=AUTHOR)['closure_revision'] == 0


def model_result():
    value = draft()
    return {key: value[key] for key in ('task_title', 'assignee', 'priority', 'deadline', 'evidence_ids')} | {'action_plan': default_plan(value)}


@pytest.mark.parametrize('fault', ['vague_legacy', 'equipment', 'root_cause', 'fake_owner', 'drop_evidence'])
def test_unstructured_or_ungrounded_model_suggestions_are_not_ai_success(setup, fault):
    repo, clock, remote, ident = setup
    output = model_result()
    if fault == 'vague_legacy':
        output.pop('action_plan')
        output['suggestion'] = '请持续关注并加强管理。'
    elif fault == 'equipment':
        output['action_plan']['objects'] = ['主反应釜', '干燥机', '纯化系统']
    elif fault == 'root_cause':
        output['action_plan']['completion_criteria'] = ['明确指出超支系由设备低负荷高能耗运行所致']
    elif fault == 'fake_owner':
        output['assignee'] = {**output['assignee'], 'name': '模型编造人'}
    else:
        output['evidence_ids'] = []
    value = repo.generate(draft(), actor=AUTHOR, llm_fn=lambda _: output)
    assert value['generation']['mode'] == 'rule_fallback'
    assert value['content']['assignee']['name'] == AUTHOR.display_name
    assert '主反应釜' not in value['content']['suggestion']


def test_structured_generation_preserves_evidence_hash_and_quarter_without_assuming_person(setup):
    repo, clock, remote, ident = setup
    source = draft()
    source['assignee'] = {}
    output = model_result()
    output['assignee'] = {'name': '', 'department': '', 'role': ''}
    value = repo.generate(source, actor=AUTHOR, llm_fn=lambda _: output)
    assert value['generation']['mode'] == 'ai'
    assert value['content']['assignee'] == output['assignee']
    assert value['content']['evidence_hashes'] == draft()['evidence_hashes']
    assert value['content']['analysis_period']['months'] == ['2026-04', '2026-05', '2026-06']
    assert len(value['generation']['source_hash']) == 64
    with pytest.raises(TaskError):
        repo.submit(value['task_id'], actor=AUTHOR, expected_version=1)


def test_reminder_authorization_is_refreshed_before_http_and_scope_is_preserved(setup):
    repo, clock, remote, ident = setup
    repo.schedule_reminders(actor=REVIEWER)
    repo.principal_resolver = lambda _: replace(REVIEWER, roles=('analyst',))
    with remote.client() as client, pytest.raises(PermissionError):
        repo.dispatch_reminders(client, actor=REVIEWER)
    assert remote.calls == []
    assert repo.notifications(actor=AUTHOR)[0]['attempts'] == 0
    outsider = replace(REVIEWER, factories=('其他厂',))
    assert repo.schedule_reminders(actor=outsider) == []
    with remote.client() as client:
        assert repo.dispatch_reminders(client, actor=outsider) == []


def test_restore_review_blocks_external_reminders_without_fabricating_delivery(setup):
    from enterprise.operations import RESTORE_REVIEW_FILE, MaintenanceError
    repo, clock, remote, ident = setup
    repo.schedule_reminders(actor=REVIEWER)
    (repo.root / RESTORE_REVIEW_FILE).write_text('{}', encoding='utf-8')
    with remote.client() as client, pytest.raises(MaintenanceError):
        repo.dispatch_reminders(client, actor=REVIEWER)
    assert not remote.calls
    assert repo.notifications(actor=AUTHOR)[0]['external_status'] == 'not_sent'


def test_read_only_or_wrong_role_cannot_schedule_reminders(setup):
    repo, clock, remote, ident = setup
    for actor in (AUTHOR, AUDITOR):
        with pytest.raises(PermissionError):
            repo.schedule_reminders(actor=actor)
        with remote.client() as client, pytest.raises(PermissionError):
            repo.dispatch_reminders(client, actor=actor)
    assert not remote.calls and repo.notifications(actor=AUTHOR) == []


def test_placeholder_person_remains_draft_instead_of_becoming_a_recipient(setup):
    repo, clock, remote, ident = setup
    value = draft()
    value['assignee']['name'] = '待指定'
    task = repo.create(value, actor=AUTHOR)
    with pytest.raises(TaskError, match='仍待指定'):
        repo.submit(task['task_id'], actor=AUTHOR, expected_version=1)


def test_mock_origin_allowlist_is_explicit_loopback_only():
    with pytest.raises(RPAPolicyError):
        RPAClient(RPAConfig(base_url='http://127.0.0.1:51234'))
    with RPAClient(RPAConfig(base_url='http://localhost:51234', mock_loopback_allowlist=('http://localhost:51234',))) as client:
        assert client.base_url == 'http://127.0.0.1:51234'
    for origin in ('http://example.com:51234', 'https://127.0.0.1:51234', 'http://127.0.0.1:51234/path', 'http://127.0.0.1:0'):
        with pytest.raises(RPAPolicyError):
            RPAClient(RPAConfig(base_url=origin, mock_loopback_allowlist=(origin,)))

# -*- coding: utf-8 -*-
"""异常案例自动暂存测试：文本组装、幂等暂存、闭环钩子（零网络零付费调用）。"""
import hashlib

from enterprise.anomaly_case import ALERT_RULE_LINE, build_case_text, case_title, stage_closed_task
from enterprise.knowledge import Repository

CONTENT = {'task_title': '核查测试产品成本差异',
           'source': {'analysis_type': '月度成本分析', 'analysis_month': '2026-05', 'product': '测试产品',
                      'finding': '材料成本环比 +15.3%，超过±10%告警阈值，原因待核查。'},
           'factories': ['测试一厂'],
           'analysis_period': {'label': '2026-05', 'months': ['2026-05']}}


def submission():
    return {'summary': '复核了采购结算单与领退料记录。', 'evaluation': '归集差异已确认并修正。',
            'evidence': [{'name': '结算单', 'reference': '归档/rec-01.csv',
                          'sha256': hashlib.sha256(b'fixture').hexdigest()}],
            'metrics': [{'name': '单位材料成本', 'before': '12.5', 'after': '12.6', 'unit': '元/盒',
                         'before_period': '2026-04', 'after_period': '2026-05',
                         'scope': '测试一厂/测试产品', 'method': '同口径复算', 'evidence_ids': ['结算单']}]}


def test_build_case_text_contains_all_closure_facts():
    text = build_case_text(CONTENT, submission(), 'TASK-1', 2, '2026-09-20T08:00:00+00:00', '验收员甲')
    for expected in ('【历史成本异常处理记录】', 'TASK-1', '测试产品', '测试一厂', '2026-05',
                     '材料成本环比 +15.3%', ALERT_RULE_LINE, '整改结果：复核了采购结算单',
                     '效果评估：归集差异已确认', '12.5 → 12.6 元/盒', '结算单', '关闭时间',
                     '验收员甲', '闭环修订：v2', '须经知识管理员确认'):
        assert expected in text


def test_stage_idempotency_and_version_reuse(tmp_path):
    repo = Repository(tmp_path / 'managed')
    first = stage_closed_task(repo.root, 'TASK-1', CONTENT, submission(),
                              1, '2026-09-20T08:00:00+00:00', '验收员甲')
    assert first['staged'] is True and first['title'] == case_title('TASK-1')
    duplicate = stage_closed_task(repo.root, 'TASK-1', CONTENT, submission(),
                                  1, '2026-09-20T08:00:00+00:00', '验收员甲')
    assert duplicate['staged'] is False and duplicate['reason'] == 'already_staged'
    # 只到"差异预览"：未确认不得产生已登记版本
    assert repo.history() == []
    # 管理员确认第一轮案例后，第二轮闭环（如返工后再次验收）复用同一文档产生新版本
    repo.commit(first['stage_id'], 'admin', '确认异常案例')
    second = stage_closed_task(repo.root, 'TASK-1', CONTENT, submission(),
                               2, '2026-10-05T08:00:00+00:00', '验收员乙')
    assert second['staged'] is True and second['doc_id'] == first['doc_id']
    repo.commit(second['stage_id'], 'admin', '确认第二轮案例')
    version = repo.get(first['doc_id'])
    assert version['category'] == '异常处理记录'
    meta = version['business_metadata']
    assert meta['evidence_role'] == 'context_only' and meta['authority'] == 'task_closure'
    assert meta['closure_revision'] == 2 and meta['task_id'] == 'TASK-1'
    assert len(repo.history()) == 2


def test_pending_stages_lists_and_commit_clears(tmp_path):
    repo = Repository(tmp_path / 'managed')
    result = stage_closed_task(repo.root, 'TASK-3', CONTENT, submission(),
                               1, '2026-09-20T08:00:00+00:00', '验收员甲')
    pending = repo.pending_stages()
    assert len(pending) == 1
    assert pending[0]['title'] == case_title('TASK-3')
    assert pending[0]['category'] == '异常处理记录'
    assert pending[0]['scope_products'] == ['测试产品']
    repo.commit(result['stage_id'], 'admin', '确认案例')
    assert repo.pending_stages() == []


def test_stage_requires_product_and_factories(tmp_path):
    bad = {**CONTENT, 'source': {**CONTENT['source'], 'product': ''}}
    result = stage_closed_task(tmp_path / 'managed', 'TASK-2', bad, submission(),
                               1, '2026-09-20T08:00:00+00:00', 'x')
    assert result['staged'] is False and result['reason'] == 'case_scope_incomplete'


def test_review_acceptance_auto_stages_case(tmp_path):
    from enterprise.task_workflow import TaskRepository
    from tests.test_task_closure import AUTHOR, REVIEWER, Clock, Remote, draft, finish_execution, material
    clock, remote = Clock(), Remote()
    principals = {actor.user_id: actor for actor in (AUTHOR, REVIEWER)}
    repo = TaskRepository(tmp_path / 'closure', clock=clock, lease_seconds=2,
                          principal_resolver=lambda ident: principals[ident])
    task = repo.create(draft(), actor=AUTHOR, task_id='TASK-AUTO-CASE')
    repo.submit(task['task_id'], actor=AUTHOR, expected_version=1)
    repo.approve(task['task_id'], actor=REVIEWER, expected_version=1)
    repo.enqueue(task['task_id'], actor=REVIEWER, expected_version=1)
    finish_execution(repo, remote, task['task_id'])
    repo.submit_rectification(task['task_id'], material(), actor=AUTHOR,
                              expected_version=1, expected_closure_revision=0)
    view = repo.review_rectification(task['task_id'], decision='accept', comment='验收通过',
                                     actor=REVIEWER, expected_version=1, expected_closure_revision=1)
    assert view['business_status'] == 'closed'
    assert view['anomaly_case']['staged'] is True
    knowledge = Repository(tmp_path / 'closure')
    # 已自动暂存但未经确认：知识库没有已登记版本，只有待确认暂存
    assert knowledge.history() == []
    # 注意 closure_revision 是提交与验收共用的计数器：首次闭环后为 2
    pending = knowledge.pending_case_stage('TASK-AUTO-CASE', view['closure_revision'])
    assert pending is not None
    assert pending['doc_id'] == view['anomaly_case']['doc_id']

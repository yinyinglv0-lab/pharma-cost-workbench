"""SQL authorization-before-pagination contracts; synthetic tmp_path DBs only.

No business data, approval, dispatch, models or network are used by these tests.
"""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import socket

import pytest

from enterprise.security import Principal
from enterprise.task_workflow import TaskError, TaskRepository

F1, F2 = "中药一厂", "中药二厂"
PRODUCT, OTHER = "测试产品A", "测试产品B"
READER = Principal("page-reader", "分页读者", ("auditor",), (F1,), (PRODUCT,))


@pytest.fixture(autouse=True)
def isolated_no_network(monkeypatch):
    monkeypatch.setenv("COST_TENANT_ID", "default")
    def forbidden(*args, **kwargs):
        raise AssertionError("Pagination tests must never open a network connection")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def repo(tmp_path):
    # Fixed time keeps the authorization/pagination contract independent of today.
    return TaskRepository(tmp_path / "synthetic-tasks", clock=lambda: 1_780_000_000.0)


def record(ident, *, factories=(F1,), product=PRODUCT, tenant="default",
           title="成本记录核查", owner="测试员工", department="生产部",
           workflow="draft", dispatch="not_sent", receipt=None,
           created="2026-07-01T00:00:00.000+00:00"):
    content = {"task_title": title, "assignee": {"name": owner, "department": department, "role": ""},
               "source": {"analysis_type": "月度成本分析", "analysis_month": "2026-05",
                          "product": product, "finding": "合成核查线索"},
               "priority": "medium", "deadline": "2026-07-30", "suggestion": "核对记录",
               "factories": list(factories), "evidence_ids": [], "analysis_run_id": "fixture"}
    text = json.dumps(content, ensure_ascii=False)
    return (ident, tenant, 1, text, hashlib.sha256(text.encode()).hexdigest(), "{}", workflow,
            dispatch, receipt, created, created, '{"user_id":"synthetic-author"}')


def seed(repo, rows):
    # This writes fixtures directly, never transitions or sends any task.
    with repo._transaction() as connection:
        connection.executemany("""INSERT INTO tasks
            (task_id,tenant_id,version,content,content_hash,generation,workflow_status,
             dispatch_status,receipt_status,created_utc,updated_utc,created_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", rows)


def ids(page):
    return [row["task_id"] for row in page["items"]]


def many_rows():
    rows = []
    for index in range(151):
        prefix = f"TASK-{index:04d}"
        rows.extend([record(prefix + "-A"),
                     record(prefix + "-B", product=OTHER),
                     record(prefix + "-C", factories=(F1, F2)),
                     record(prefix + "-D", tenant="other-tenant")])
    return rows


def test_151_authorized_rows_are_complete_without_filtering_after_paging(repo, monkeypatch):
    seed(repo, many_rows())
    views, scope_calls = [], []
    original = repo._view
    import enterprise.task_workflow as module
    original_scope = module._scope
    def view(connection, row):
        views.append(row["task_id"])
        return original(connection, row)
    def scope(*args, **kwargs):
        scope_calls.append(args[2])
        return original_scope(*args, **kwargs)
    monkeypatch.setattr(repo, "_view", view)
    monkeypatch.setattr(module, "_scope", scope)
    first = repo.page(actor=READER)
    second = repo.page(actor=READER, offset=100)
    assert first["total"] == second["total"] == 151
    assert first["limit"] == second["limit"] == 100
    assert first["offset"] == 0 and second["offset"] == 100
    expected = [f"TASK-{i:04d}-A" for i in range(151)]
    assert ids(first) + ids(second) == expected
    assert len(first["items"]) == 100 and len(second["items"]) == 51
    assert views == expected and len(scope_calls) == 151
    assert repo.page(actor=READER, offset=5000) == {"items": [], "total": 151, "offset": 5000, "limit": 100}
    assert [item["task_id"] for item in repo.list(actor=READER)] == expected[:100]
    assert [item["task_id"] for item in repo.list(actor=READER, offset=150)] == expected[-1:]


@pytest.mark.parametrize("factories,products,total", [
    ((F1,), (PRODUCT,), 151), ((F1, F2), (PRODUCT,), 302),
    ((F1,), (PRODUCT, OTHER), 302), ((F1, F2), (PRODUCT, OTHER), 453),
    ((F2,), (PRODUCT,), 0), (("*",), (PRODUCT,), 302),
    ((F1,), ("*",), 302), (("*",), ("*",), 453),
    ((), (PRODUCT,), 0), ((F1,), (), 0),
    ((F1, None), (PRODUCT,), 151), ((F1,), (PRODUCT, None), 151),
    ((None,), (PRODUCT,), 0), ((F1,), (None,), 0),
])
def test_total_and_summary_have_identical_all_factory_product_scope(repo, factories, products, total):
    seed(repo, many_rows())
    actor = replace(READER, factories=factories, products=products)
    page = repo.page(actor=actor, limit=5)
    assert page["total"] == total
    assert len(page["items"]) == min(5, total)
    assert repo.summary(actor=actor)["generated"] == total
    for item in page["items"]:
        assert item["tenant_id"] == "default"
        assert "*" in factories or set(item["content"]["factories"]) <= set(factories)
        assert "*" in products or item["content"]["source"]["product"] in products


def test_other_tenant_is_not_counted_and_principal_tenant_requires_configuration(repo, monkeypatch):
    seed(repo, many_rows())
    outsider = replace(READER, tenant_id="other-tenant")
    with pytest.raises(PermissionError):
        repo.page(actor=outsider)
    with pytest.raises(PermissionError):
        repo.summary(actor=outsider)
    monkeypatch.setenv("COST_TENANT_ID", "other-tenant")
    page = repo.page(actor=outsider, limit=1000)
    assert page["total"] == 151
    assert all(item["task_id"].endswith("-D") for item in page["items"])
    assert repo.summary(actor=outsider)["generated"] == 151


@pytest.mark.parametrize("actor", [None, "auditor", {"roles": ["auditor"]}, replace(READER, roles=("system_admin",))])
def test_untrusted_or_wrong_role_cannot_read_or_initialize_database(repo, actor):
    for method in (repo.page, repo.list, repo.summary):
        with pytest.raises(PermissionError):
            method(actor=actor)
    assert not repo.db.exists()


def test_status_union_preserves_workflow_dispatch_and_receipt_precedence(repo):
    seed(repo, [record("TASK-DRAFT"),
                record("TASK-PENDING", workflow="issued", dispatch="pending"),
                record("TASK-SENT", workflow="issued", dispatch="accepted", receipt="sent"),
                record("TASK-RECEIVED", workflow="issued", dispatch="accepted", receipt="received"),
                record("TASK-COMPLETE", workflow="issued", dispatch="accepted", receipt="completed"),
                record("TASK-UNKNOWN", workflow="issued", dispatch="unknown"),
                record("TASK-OVERDUE", workflow="issued", dispatch="accepted", receipt="overdue"),
                record("TASK-EMPTY-RECEIPT", workflow="submitted", receipt="")])
    expected = {
        "draft": {"TASK-DRAFT"}, "pending": {"TASK-PENDING"}, "sent": {"TASK-SENT"},
        "received": {"TASK-RECEIVED"}, "completed": {"TASK-COMPLETE"},
        "unknown": {"TASK-UNKNOWN"}, "overdue": {"TASK-OVERDUE"},
        "not_sent": {"TASK-DRAFT", "TASK-EMPTY-RECEIPT"},
        "accepted": {"TASK-SENT", "TASK-RECEIVED", "TASK-COMPLETE", "TASK-OVERDUE"},
        "issued": {"TASK-PENDING", "TASK-SENT", "TASK-RECEIVED", "TASK-COMPLETE", "TASK-UNKNOWN", "TASK-OVERDUE"},
        "submitted": {"TASK-EMPTY-RECEIPT"}, "": set(), "not-a-status": set(),
    }
    for status, matches in expected.items():
        page = repo.page(actor=READER, status=status, limit=2)
        assert page["total"] == len(matches)
        assert ids(page) == sorted(matches)[:2]
        assert {item["task_id"] for item in repo.list(actor=READER, status=status)} == matches
        assert repo.summary(actor=READER, status=status)["generated"] == len(matches)
    assert repo.summary(actor=READER) == {"generated": 8, "accepted": 4, "received": 2, "confirmed": 1,
                                        "execution_completed": 1, "completed": 0, "closed": 0, "pending_acceptance": 0,
                                        "business_overdue": 0, "needs_attention": 2, "simulated": True}


@pytest.mark.parametrize("field,query", [
    ("title", "75%_稳定"), ("owner", "测试员工乙"), ("department", "成本核查部"),
    ("product", OTHER), ("title", "x' OR 1=1 --"), ("title", "ASCII"),
])
def test_literal_query_matches_only_selected_fields_before_count_and_page(repo, field, query):
    actor = replace(READER, products=(PRODUCT, OTHER))
    seed(repo, [record("TASK-MATCH", **{field: query}), record("TASK-NOMATCH", title="75XYZ稳定"),
                record("TASK-HIDDEN", factories=(F2,), **{field: query})])
    value = query.lower() if query == "ASCII" else query
    page = repo.page(actor=actor, query="  " + value + "  ", limit=1)
    assert page["total"] == 1 and ids(page) == ["TASK-MATCH"]
    assert repo.summary(actor=actor, query=value)["generated"] == 1
    assert [item["task_id"] for item in repo.list(actor=actor, query=value)] == ["TASK-MATCH"]
    assert repo.page(actor=actor, query=value, offset=1)["items"] == []


def test_query_id_empty_and_status_combination(repo):
    seed(repo, [record("TASK-ID-ALPHA", title="同标题"),
                record("TASK-ID-BETA", title="同标题", workflow="submitted")])
    assert ids(repo.page(actor=READER, query="id-alpha")) == ["TASK-ID-ALPHA"]
    for query in (None, "", "  "):
        assert repo.page(actor=READER, query=query)["total"] == 2
    assert ids(repo.page(actor=READER, query="同标题", status="submitted")) == ["TASK-ID-BETA"]
    assert repo.summary(actor=READER, query="同标题", status="draft")["generated"] == 1


@pytest.mark.parametrize("parameters", [
    {"limit": 0}, {"limit": 1001}, {"limit": True}, {"limit": 1.0}, {"limit": "1"},
    {"offset": -1}, {"offset": True}, {"offset": 1.0}, {"offset": "0"}, {"offset": 2**63},
    {"query": 42}, {"query": "x" * 201}, {"query": "a\x00b"},
    {"status": 1}, {"status": "s" * 65}, {"status": "x\x00"},
])
def test_invalid_paging_and_filters_fail_before_database_initialization(repo, parameters):
    with pytest.raises(TaskError):
        repo.page(actor=READER, **parameters)
    assert not repo.db.exists()


def test_summary_uses_aggregate_without_materializing_outbox_views(repo, monkeypatch):
    seed(repo, many_rows())
    def forbidden(*args, **kwargs):
        raise AssertionError("Summary must not build individual task/outbox views")
    monkeypatch.setattr(repo, "_view", forbidden)
    assert repo.summary(actor=READER) == {"generated": 151, "accepted": 0, "received": 0,
        "confirmed": 0, "execution_completed": 0, "completed": 0, "closed": 0, "pending_acceptance": 0,
        "business_overdue": 0, "needs_attention": 0, "simulated": True}
    assert repo.summary(actor=READER, query="never") == {"generated": 0, "accepted": 0, "received": 0,
        "confirmed": 0, "execution_completed": 0, "completed": 0, "closed": 0, "pending_acceptance": 0,
        "business_overdue": 0, "needs_attention": 0, "simulated": True}


def test_scope_defense_rejects_discrepancy_instead_of_thinning_page(repo, monkeypatch):
    seed(repo, [record("TASK-ONE")])
    import enterprise.task_workflow as module
    def rejected(*args, **kwargs):
        raise PermissionError("Synthetic changed authorization")
    monkeypatch.setattr(module, "_scope", rejected)
    with pytest.raises(PermissionError):
        repo.page(actor=READER)


def test_tenant_order_index_and_created_tie_order(repo):
    seed(repo, [record("TASK-B"), record("TASK-A"),
                record("TASK-Z", created="2026-07-02T00:00:00.000+00:00")])
    assert ids(repo.page(actor=READER)) == ["TASK-Z", "TASK-A", "TASK-B"]
    with closing(repo._connect()) as connection:
        indexes = connection.execute("PRAGMA index_list('tasks')").fetchall()
        assert "tasks_tenant_created" in {row["name"] for row in indexes}
        plans = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM tasks WHERE tenant_id=? ORDER BY created_utc DESC,task_id LIMIT 1", ("default",)).fetchall()
        assert any("tasks_tenant_created" in row["detail"] for row in plans)

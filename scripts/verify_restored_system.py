#!/usr/bin/env python3
"""Read-only acceptance of a NEW restored system against a private assessment.

Never restores, dispatches, synchronizes receipts, clears a gate, loads a model or
regenerates a report. --output is the only write. Keep all restored writers stopped.
The assessment's sibling SCENARIO/approval.json files must remain available; their
recorded hashes authenticate the approval comparison without copying their content.
Defaults (three scenarios, 77 dense chunks, dimension 1024) describe the recorded
acceptance run. Indexed version counts come from the verified release manifest;
logical documents are counted separately by distinct doc_id. Historical versions
remain intact. --expected-versions (alias --expected-documents) asserts the number
of confirmed versions, not the number of logical documents or bootstrap files.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from enterprise.operations import (MaintenanceError, RESTORE_REVIEW_FILE,
    assert_dispatch_allowed, inspect_managed_integrity, sha256_file)

GOLDENS = {'monthly_yinhuang': Decimal('650180'), 'quarterly_banlangen': Decimal('2124860'),
           'topic_liuwei': Decimal('595700')}
MAX_JSON_BYTES = 8 * 1024 * 1024


class VerificationError(ValueError):
    pass


def _require(condition, code):
    if not condition:
        raise VerificationError(code)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value):
    return _sha(_canonical(value).encode('utf-8'))


def _json(path):
    _require(path.is_file() and path.stat().st_size <= MAX_JSON_BYTES, 'json_missing_or_too_large')
    return json.loads(path.read_text(encoding='utf-8'))


def _ro(path):
    _require(path.is_file(), 'database_missing')
    # The target must be an offline restored copy, not a concurrently written DB.
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    return connection


def _tree_hashes(target):
    result = {}
    for folder, directories, files in os.walk(target, followlinks=False):
        for name in directories + files:
            path = Path(folder) / name
            _require(not path.is_symlink() and not (hasattr(path, 'is_junction') and path.is_junction()), 'linked_restore_path')
        for name in files:
            path = Path(folder) / name
            _require(not name.endswith('-wal') or path.stat().st_size == 0, 'restore_has_live_wal')
            result[path.relative_to(target).as_posix()] = sha256_file(path)
    return result


def _baseline_artifact(assessment_path, scenario, kind):
    entry = scenario['artifacts'][kind]
    scenario_id = scenario['scenario_id']
    _require(re.fullmatch(r'[a-z0-9_]{1,80}', scenario_id) is not None, 'invalid_scenario_id')
    name = entry['file']
    _require(isinstance(name, str) and Path(name).name == name and '\\' not in name and ':' not in name,
             'unsafe_baseline_artifact')
    path = assessment_path.parent / scenario_id / name
    _require(path.resolve().is_relative_to(assessment_path.parent.resolve()), 'baseline_path_escape')
    raw = path.read_bytes()
    _require(len(raw) == entry['bytes'] and _sha(raw) == entry['sha256'], 'baseline_artifact_hash_mismatch')
    return json.loads(raw)


def _gate(target):
    managed = target / 'managed'
    marker = _json(managed / RESTORE_REVIEW_FILE)
    report = _json(target / 'restore_report.json')
    manifest = _json(target / 'restore_manifest.json')
    _require(marker.get('dispatch_blocked') is True and report.get('dispatch_blocked') is True,
             'restore_gate_not_closed')
    _require(marker.get('backup_id') == report.get('backup_id') == manifest.get('backup_id')
             and re.fullmatch('[0-9a-f]{32}', str(marker.get('backup_id', ''))), 'restore_identity_mismatch')
    blocked = False
    try:
        assert_dispatch_allowed(managed)
    except MaintenanceError:
        blocked = True
    _require(blocked, 'dispatch_not_blocked')
    return {'backup_id': marker['backup_id'], 'dispatch_blocked': True, 'gate_cleared': False}


def _knowledge(managed, scenarios, expected_chunks, expected_documents, expected_dimension):
    releases = {ident for row in scenarios for ident in row['provenance']['index_releases']}
    _require(len(releases) == 1, 'expected_one_baseline_release')
    release_id = next(iter(releases))
    _require(re.fullmatch('[0-9a-f]{32}', release_id) is not None, 'invalid_release_id')
    with closing(_ro(managed / 'knowledge_releases.db')) as connection:
        row = connection.execute('SELECT * FROM releases WHERE release_id=?', (release_id,)).fetchone()
        active = connection.execute('SELECT release_id FROM active_release WHERE singleton=1').fetchone()
    _require(row is not None and row['status'] == 'published', 'baseline_release_not_published')
    _require(active is not None and active[0] == release_id, 'active_release_differs_from_baseline')
    _require(_sha(row['manifest'].encode('utf-8')) == row['manifest_sha256'], 'release_manifest_hash_mismatch')
    manifest = json.loads(row['manifest'])
    _require(manifest.get('release_id') == release_id and manifest.get('status') == 'published', 'release_manifest_identity_mismatch')
    _require(manifest.get('embedding', {}).get('status') == 'ready', 'dense_embedding_not_ready')
    input_ids = manifest['input_version_ids']
    manifest_version_ids = [item['version_id'] for item in manifest['documents']]
    logical_doc_ids = {item['doc_id'] for item in manifest['documents']}
    _require(bool(input_ids) and len(input_ids) == len(set(input_ids)) == len(manifest_version_ids)
             and set(input_ids) == set(manifest_version_ids), 'manifest_version_set_mismatch')
    _require(all(isinstance(ident, str) and ident for ident in logical_doc_ids), 'manifest_document_id_missing')
    if expected_documents is not None:  # Retained API name; counts confirmed versions.
        _require(len(input_ids) == expected_documents, 'release_version_count_mismatch')
    _require(manifest['embedding'].get('dimension') == expected_dimension, 'embedding_dimension_mismatch')
    artifact = managed / 'knowledge_releases' / release_id / 'index.sqlite'
    expected_hash = manifest['artifacts']['index.sqlite']['sha256']
    _require(sha256_file(artifact) == expected_hash, 'index_artifact_hash_mismatch')
    with closing(_ro(artifact)) as connection:
        generation = connection.execute('SELECT release_id FROM generation').fetchall()
        ids = [item[0] for item in connection.execute('SELECT chunk_id FROM chunks ORDER BY chunk_id')]
        vector_count = connection.execute('SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL').fetchone()[0]
        artifact_ids = {item[0] for item in connection.execute('SELECT DISTINCT version_id FROM chunks')}
        for (text,) in connection.execute('SELECT vector FROM chunks WHERE vector IS NOT NULL'):
            vector = json.loads(text)
            _require(isinstance(vector, list) and len(vector) == expected_dimension, 'vector_dimension_mismatch')
    indexed_versions = len(artifact_ids)
    _require([item[0] for item in generation] == [release_id], 'index_generation_mismatch')
    _require(len(ids) == vector_count == expected_chunks and artifact_ids == set(input_ids), 'dense_chunk_count_mismatch')
    _require(manifest['chunk_ids_sha256'] == _digest(ids), 'chunk_ids_hash_mismatch')
    return {'release_id': release_id, 'status': 'published', 'embedding_status': 'ready',
            'logical_documents': len(logical_doc_ids), 'indexed_confirmed_versions': indexed_versions,
            'manifest_confirmed_versions': len(manifest_version_ids),
            'version_count_basis': 'verified_manifest_and_index',
            'chunks': len(ids), 'vectors': vector_count, 'vector_dimension': expected_dimension,
            'manifest_sha256': row['manifest_sha256'], 'index_sha256': expected_hash}


def _report(managed, assessment_path, scenario):
    from enterprise.report_records import ReportRepository
    from enterprise.security import Principal

    class ReadOnlyReports(ReportRepository):
        def _connect(self):
            return _ro(self.db)

    # This CLI is an offline OS-admin verification, not an authentication bypass
    # exposed to users. No credential, current model config or network is consulted.
    actor = Principal('restore-verifier', 'Restore verifier', ('auditor',), ('*',), ('*',))
    repository = ReadOnlyReports(managed)
    record = repository.get(scenario['report_id'], actor=actor)
    payload = record['payload']
    _require(record['status'] == 'approved' and record['hash'] == scenario['frozen_hash']
             and payload['frozen_hash'] == scenario['frozen_hash'], 'report_approval_or_hash_mismatch')
    approval = _baseline_artifact(assessment_path, scenario, 'approval')
    for field in ('id', 'tenant', 'hash', 'status', 'version', 'approver', 'approved'):
        _require(record[field] == approval[field], 'report_approval_baseline_mismatch')
    _require(payload['versions'] == scenario['provenance']['versions'], 'report_provenance_mismatch')
    exports = {}
    with closing(_ro(managed / 'reports.db')) as connection:
        for format in ('docx', 'pdf'):
            artifact = connection.execute('SELECT hash,content FROM report_artifacts WHERE report_id=? AND format=? AND version=?',
                (record['id'], format, record['version'])).fetchone()
            _require(artifact is not None, 'cached_report_export_missing')
            raw = artifact['content']
            baseline = scenario['artifacts'][format]
            digest = _sha(raw)
            _require(digest == artifact['hash'] == baseline['sha256'] and len(raw) == baseline['bytes'], 'cached_report_export_mismatch')
            _require(raw.startswith(b'%PDF-') if format == 'pdf' else raw.startswith(b'PK'), 'cached_report_format_mismatch')
            exports[format] = {'sha256': digest, 'bytes': len(raw), 'from_cache': True, 'regenerated': False}
    return {'report_id': record['id'], 'status': record['status'], 'version': record['version'],
            'frozen_hash': record['hash'], 'exports': exports}, payload


def _source_refs(value):
    if isinstance(value, dict):
        if isinstance(value.get('file'), str) and isinstance(value.get('sha256'), str):
            name = PureWindowsPath(value['file']).name
            if name.startswith('中药一厂_成本汇总_2026') and name.endswith('.csv'):
                yield name, value['sha256']
        for child in value.values():
            yield from _source_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _source_refs(child)


def _golden(data, scenario, payload):
    import pandas as pd
    refs = set(_source_refs(payload))
    _require(bool(refs), 'frozen_cost_source_reference_missing')
    frames = []
    hashes = []
    for name, expected in sorted(refs):
        candidates = [data / name, data / 'data_upload' / name]
        matches = [path for path in candidates if path.is_file() and sha256_file(path) == expected]
        _require(bool(matches), 'restored_cost_source_hash_missing')
        path = matches[0]
        try:
            frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding='utf-8-sig')
        except UnicodeDecodeError:
            frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding='gb18030')
        if '工厂' not in frame:
            frame['工厂'] = '中药一厂'  # The explicit source contract identifies this factory.
        frames.append(frame)
        hashes.append(expected)
    frame = pd.concat(frames, ignore_index=True)
    months = payload['period']['months']
    params = payload['params']
    selected = frame[frame['工厂'].eq('中药一厂') & frame['产品名称'].eq(params['product'])
                     & frame['产品规格'].eq(params['specification']) & frame['月份'].isin(months)]
    columns = ['工厂', '产品名称', '产品规格', '月份', '总成本(元)', '产量(盒)']
    selected = selected[columns].drop_duplicates()
    _require(len(selected) == len(months) and set(selected['月份']) == set(months), 'cost_month_missing_or_ambiguous')
    total = sum((Decimal(value) for value in selected['总成本(元)']), Decimal(0))
    volume = sum((Decimal(value) for value in selected['产量(盒)']), Decimal(0))
    expected = GOLDENS[scenario['scenario_id']]
    _require(Decimal(str(scenario['numeric_checks']['competition_golden']['expected_total_cost'])) == expected,
             'assessment_golden_changed')
    _require(abs(total - expected) <= Decimal('.01') and abs(total - Decimal(str(payload['facts']['current']['total_cost']))) <= Decimal('.01'), 'cost_golden_mismatch')
    _require(volume == Decimal(str(payload['facts']['current']['volume'])), 'cost_volume_mismatch')
    return {'total_cost': str(total), 'expected_total_cost': str(expected), 'volume': str(volume),
            'months_checked': len(months), 'source_sha256': sorted(set(hashes))}


def _task(managed, scenario, baseline):
    ident = scenario['task']['task_id']
    _require(baseline['task_id'] == ident, 'task_baseline_mismatch')
    with closing(_ro(managed / 'task_workflow.db')) as connection:
        task = connection.execute('SELECT * FROM tasks WHERE task_id=?', (ident,)).fetchone()
        _require(task is not None, 'task_missing')
        for field, expected in (('workflow_status', 'issued'), ('dispatch_status', 'accepted'), ('receipt_status', 'completed')):
            _require(task[field] == expected == baseline[field], 'task_terminal_state_mismatch')
        _require(task['version'] == task['approved_version'] == baseline['outbox']['version']
                 and task['approved_hash'] == task['content_hash'] == _sha(task['content'].encode('utf-8')), 'task_approval_hash_mismatch')
        versions = connection.execute('SELECT version,content,content_hash FROM task_versions WHERE task_id=? ORDER BY version', (ident,)).fetchall()
        _require([row['version'] for row in versions] == list(range(1, task['version'] + 1)), 'task_version_history_incomplete')
        _require(all(_sha(row['content'].encode('utf-8')) == row['content_hash'] for row in versions)
                 and versions[-1]['content_hash'] == task['approved_hash'], 'task_version_hash_mismatch')
        outbox = connection.execute('SELECT * FROM outbox WHERE task_id=? AND version=?', (ident, task['approved_version'])).fetchone()
        _require(outbox is not None and outbox['status'] == baseline['outbox']['status'] == 'delivered'
                 and outbox['idempotency_key'] == baseline['outbox']['idempotency_key'], 'outbox_identity_mismatch')
        _require(_sha(outbox['payload'].encode('utf-8')) == outbox['payload_hash'], 'outbox_payload_hash_mismatch')
        expected_receipt = _digest(baseline['receipt'])
        _require(_digest(json.loads(task['receipt'])) == expected_receipt, 'task_receipt_mismatch')
        receipts = connection.execute('SELECT version,data FROM task_receipts WHERE task_id=?', (ident,)).fetchall()
        _require(any(row['version'] == task['approved_version'] and _digest(json.loads(row['data'])) == expected_receipt
                     for row in receipts), 'completed_receipt_history_missing')
    return {'task_id': ident, 'workflow_status': 'issued', 'dispatch_status': 'accepted', 'receipt_status': 'completed',
            'versions': len(versions), 'receipt_records': len(receipts), 'approved_hash': task['approved_hash'],
            'payload_hash': outbox['payload_hash'], 'receipt_sha256': expected_receipt, 'outbox_status': 'delivered'}


def _cost_revision(managed, scenarios):
    baselines = [row['provenance']['versions'].get('upstream', {}).get('cost_revision') for row in scenarios]
    available = [entry for entry in baselines if isinstance(entry, dict) and 'revision' in entry and 'hash' in entry]
    for entry in available:
        _require(type(entry['revision']) is int and entry['revision'] >= 0
                 and isinstance(entry['hash'], str) and re.fullmatch('[0-9a-f]{64}', entry['hash']),
                 'baseline_cost_revision_invalid')
    captured = [dict(revision=revision, hash=digest) for revision, digest in
                sorted({(entry['revision'], entry['hash']) for entry in available})]
    persisted = [entry for entry in available if entry['revision'] > 0]
    loader_baselines = len(available) - len(persisted)
    path = managed / 'cost_versions.db'
    if not path.exists():
        _require(not persisted, 'baseline_cost_revision_database_missing')
        return {'status': 'not_available', 'reason': 'no_managed_cost_database; source CSV goldens checked separately',
                'captured_baselines': captured, 'loader_baseline_scenarios': loader_baselines}
    with closing(_ro(path)) as connection:
        rows = connection.execute('SELECT id,hash,data FROM revisions ORDER BY id').fetchall()
    _require(all(_sha(row['data'].encode('utf-8')) == row['hash'] for row in rows), 'cost_revision_hash_mismatch')
    for entry in persisted:
        _require(any(row['id'] == entry['revision'] and row['hash'] == entry['hash'] for row in rows), 'baseline_cost_revision_missing')
    complete = len(persisted) == len(scenarios)
    reason = ('revision 0 is a loader baseline, not a persisted revision; source CSV hashes and goldens checked separately'
              if loader_baselines else 'assessment does not record a comparable cost revision ID/hash')
    return {'status': 'verified' if complete else 'not_available', 'reason': None if complete else reason,
            'captured_baselines': captured, 'loader_baseline_scenarios': loader_baselines,
            'persisted_baselines_verified': len(persisted),
            'self_hashes_verified': len(rows), 'latest_revision': rows[-1]['id'] if rows else None,
            'latest_hash': rows[-1]['hash'] if rows else None}


def verify_restored_system(target, assessment, *, expected_chunks=77, expected_documents=None, expected_dimension=1024):
    target = Path(target).absolute()
    assessment = Path(assessment).resolve()
    _require(target.resolve() == target and target.is_dir(), 'target_must_be_real_restore_directory')
    _require((target / 'data').is_dir() and (target / 'managed').is_dir(), 'restore_roots_missing')
    from paths import DATA_DIR, MANAGED_DIR
    _require(target != Path(DATA_DIR).resolve() and target / 'managed' != Path(MANAGED_DIR).resolve(), 'refuse_current_live_root')
    document = _json(assessment)
    _require(document.get('schema_version') == 'acceptance-scenarios/1.0', 'unsupported_assessment')
    scenarios = document.get('scenarios', [])
    _require(len(scenarios) == 3 and {row['scenario_id'] for row in scenarios} == set(GOLDENS), 'three_scenario_baseline_required')
    receipts = document['actualmockreceipts']['receipts']
    receipt_by_id = {row['task_id']: row for row in receipts}
    _require(len(receipt_by_id) == 3 and document['actualmockreceipts'].get('all_completed') is True,
             'completed_mock_baseline_required')
    before = _tree_hashes(target)
    result = {'schema_version': 'restored-system-verification/1.0', 'assessment_sha256': sha256_file(assessment),
              'run_id': document.get('run_id'), 'started_at': datetime.now(timezone.utc).isoformat(),
              'network_requests': 0, 'reports_regenerated': 0, 'dispatch_attempts': 0, 'gate_cleared': False,
              'assessment_model_used_count': document.get('model_used', {}).get('used_count'),
              'assessment_model_status': document.get('model_used', {}).get('status'), 'checks': []}
    deadline = time.monotonic() + 180

    def check(name, action):
        try:
            _require(time.monotonic() < deadline, 'verification_time_budget_exceeded')
            details = action()
            result['checks'].append({'name': name, 'status': 'pass', 'details': details})
            return details
        except Exception as exc:
            error = str(exc) if isinstance(exc, VerificationError) else type(exc).__name__
            result['checks'].append({'name': name, 'status': 'fail', 'error': error})
            return None

    check('restore_gate', lambda: _gate(target))
    def integrity():
        value = inspect_managed_integrity(target / 'managed', deep=True)
        _require(not value['errors'], 'managed_integrity_failed')
        return {key: value[key] for key in ('databases_checked', 'blobs_checked', 'payloads_checked')}
    check('managed_integrity', integrity)
    check('knowledge_release', lambda: _knowledge(target / 'managed', scenarios, expected_chunks, expected_documents, expected_dimension))
    for scenario in scenarios:
        ident = scenario['scenario_id']
        def report_check(scenario=scenario):
            summary, payload = _report(target / 'managed', assessment, scenario)
            summary['golden'] = _golden(target / 'data', scenario, payload)
            return summary
        check('report.' + ident, report_check)
        check('task.' + ident, lambda scenario=scenario: _task(target / 'managed', scenario, receipt_by_id[scenario['task']['task_id']]))
    check('cost_revision', lambda: _cost_revision(target / 'managed', scenarios))
    def unchanged():
        after = _tree_hashes(target)
        _require(before == after, 'restored_files_changed_during_verification')
        return {'files_checked': len(before), 'content_unchanged': True}
    check('read_only', unchanged)
    result['ok'] = all(row['status'] == 'pass' for row in result['checks'])
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--assessment', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-chunks', type=int, default=77)
    parser.add_argument('--expected-versions', '--expected-documents', dest='expected_documents', type=int,
                        help='Optional confirmed-version count assertion, not distinct logical documents; default uses verified manifest')
    parser.add_argument('--expected-dimension', type=int, default=1024)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    target = args.target.resolve()
    if output.is_relative_to(target) or output == args.assessment.resolve() or output.exists():
        parser.error('--output must be a new file outside the restored root and assessment')
    if (not output.parent.is_dir() or args.expected_chunks < 1 or args.expected_dimension < 1
            or (args.expected_documents is not None and args.expected_documents < 1)):
        parser.error('output parent must exist and expected counts must be positive')
    try:
        report = verify_restored_system(args.target, args.assessment,
            expected_chunks=args.expected_chunks, expected_documents=args.expected_documents, expected_dimension=args.expected_dimension)
    except Exception as exc:
        report = {'schema_version': 'restored-system-verification/1.0', 'ok': False,
                  'error': str(exc) if isinstance(exc, VerificationError) else type(exc).__name__,
                  'network_requests': 0, 'dispatch_attempts': 0, 'gate_cleared': False}
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps({'ok': report['ok'], 'checks': len(report.get('checks', [])),
                      'network_requests': 0, 'reports_regenerated': 0, 'gate_cleared': False}))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

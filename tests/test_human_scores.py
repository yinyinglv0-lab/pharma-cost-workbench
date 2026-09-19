"""Human rating validation uses synthetic files only; it never scores real reports."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import socket

import pytest

from scripts import validate_human_scores as validator

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
REVIEWED = '2026-09-19T17:30:00+08:00'
COMMENT = '已逐项核对成本归因的证据链、对标口径与结论边界。'


def save_rows(assessment, rows, *, header=None, bom=False):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(validator.COLUMNS if header is None else header)
    for row in rows:
        writer.writerow([row.get(key, '') for key in validator.COLUMNS] if isinstance(row, dict) else row)
    raw = stream.getvalue().encode('utf-8-sig' if bom else 'utf-8')
    (assessment.parent / 'human_scores.csv').write_bytes(raw)


def complete(row, attribution='0', benchmark='5'):
    row.update(attribution_score_0_to_5=attribution, benchmark_score_0_to_5=benchmark,
               reviewer='合成测试评审甲', reviewed_at=REVIEWED, comments=COMMENT)


@pytest.fixture
def scoring_inputs(tmp_path):
    scenarios = [
        {'scenario_id': 'monthly', 'product': '合成产品甲', 'theme': '月度成本分析',
         'report_id': 'CB-SYNTH-1', 'frozen_hash': 'a' * 64},
        {'scenario_id': 'quarterly', 'product': '合成产品乙', 'theme': '季度成本分析',
         'report_id': 'CB-SYNTH-2', 'frozen_hash': 'b' * 64},
        {'scenario_id': 'topic', 'product': '合成产品丙', 'theme': '专题分析',
         'report_id': 'CB-SYNTH-3', 'frozen_hash': 'c' * 64},
    ]
    document = {'schema_version': 'acceptance-scenarios/1.0', 'run_id': 'synthetic-human-review',
        'human_score_status': 'not_scored', 'scenarios': scenarios,
        'model_used': {'used_count': 0, 'attempted_modules': 12, 'all_requested_validated': False, 'status': 'degraded'}}
    assessment = tmp_path / 'assessment.json'
    assessment.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
    rows = [dict(zip(validator.COLUMNS, [row['scenario_id'], row['product'], row['theme'], row['report_id'],
            '', '', '', '', '', '待人工审核（评分留空，合法范围0–5）'])) for row in scenarios]
    save_rows(assessment, rows)
    return assessment, rows


def errors(report):
    return [row['code'] for row in report['errors']]


def test_blank_bom_remains_pending_not_zero_or_authenticated(scoring_inputs, monkeypatch):
    assessment, rows = scoring_inputs
    save_rows(assessment, rows, bom=True)
    def forbidden(*args, **kwargs):
        raise AssertionError('Scoring validation must not open a network connection')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    before = {path.name: path.read_bytes() for path in assessment.parent.iterdir()}
    report = validator.validate_human_scores(assessment, now=NOW)
    assert report['valid'] and not report['complete']
    assert report['status'] == 'pending'
    assert report['completion_fraction'] == '0/3'
    assert report['completed_scenarios'] == report['score_values_recorded'] == 0
    assert report['pending_scenarios'] == 3
    assert all(value is None for value in report['averages'].values())
    assert all(row[key] is None for row in report['scenarios'] for key in validator.SCORE_FIELDS)
    assert report['identity_verification'] == 'not_verified'
    assert report['rating_authenticity_verified'] is False
    assert report['assessment_model_result'] == json.loads(before['assessment.json'])['model_used']
    assert report['inputs']['assessment']['sha256'] == hashlib.sha256(before['assessment.json']).hexdigest()
    assert report['inputs']['human_scores_csv']['sha256'] == hashlib.sha256(before['human_scores.csv']).hexdigest()
    assert [row['report_frozen_hash'] for row in report['scenarios']] == ['a' * 64, 'b' * 64, 'c' * 64]
    assert before == {path.name: path.read_bytes() for path in assessment.parent.iterdir()}


def test_zero_five_and_decimals_are_complete_with_exact_decimal_averages(scoring_inputs):
    assessment, rows = scoring_inputs
    for row, scores in zip(rows, [('0', '5'), ('5.000', '0'), ('2.50', '4.25')]):
        complete(row, *scores)
    save_rows(assessment, list(reversed(rows)))  # Input order need not match assessment order.
    report = validator.validate_human_scores(assessment, now=NOW)
    assert report['valid'] and report['complete']
    assert report['status'] == 'human_ratings_recorded'
    assert report['completion_fraction'] == '3/3'
    assert report['score_values_recorded'] == 6
    assert report['averages'] == {'attribution_score_0_to_5': '2.5', 'benchmark_score_0_to_5': '3.083333',
                                  'all_scores_0_to_5': '2.791667'}
    assert report['scenarios'][0]['attribution_score_0_to_5'] == '0'
    assert report['scenarios'][0]['benchmark_score_0_to_5'] == '5'
    assert all(row['reviewed_at_utc'] == '2026-09-19T09:30:00+00:00' for row in report['scenarios'])
    assert all(row['identity_verified'] is False for row in report['scenarios'])
    assert report['assessment_model_result']['used_count'] == 0


def test_partial_set_averages_only_complete_rows(scoring_inputs):
    assessment, rows = scoring_inputs
    complete(rows[1], '0', '5')
    save_rows(assessment, rows)
    report = validator.validate_human_scores(assessment, now=NOW)
    assert report['valid'] and not report['complete']
    assert report['status'] == 'pending'
    assert report['completed_scenarios'] == 1 and report['pending_scenarios'] == 2
    assert report['score_values_recorded'] == 2
    assert report['averages'] == {'attribution_score_0_to_5': '0', 'benchmark_score_0_to_5': '5', 'all_scores_0_to_5': '2.5'}


@pytest.mark.parametrize('score', ['NaN', 'sNaN', 'Infinity', '-Infinity', 'Inf', '5.01', '-0.01', 'six', '1e0', '1e9999', '1,5'])
def test_nonfinite_out_of_range_or_non_decimal_scores_are_invalid(scoring_inputs, score):
    assessment, rows = scoring_inputs
    complete(rows[0], score, '4')
    save_rows(assessment, rows)
    report = validator.validate_human_scores(assessment, now=NOW)
    assert not report['valid'] and report['status'] == 'invalid_human_scores'
    assert report['errors'][0]['field'] == 'attribution_score_0_to_5'
    assert report['completed_scenarios'] is None
    assert all(value is None for value in report['averages'].values())


@pytest.mark.parametrize('field,value', [
    ('scenario_id', 'unknown'), ('report_id', 'CB-WRONG'), ('product', 'wrong'), ('analysis_theme', 'wrong'),
])
def test_identity_must_match_assessment_exactly(scoring_inputs, field, value):
    assessment, rows = scoring_inputs
    rows[0][field] = value
    save_rows(assessment, rows)
    report = validator.validate_human_scores(assessment, now=NOW)
    assert not report['valid']
    assert report['errors'][0]['field'] == field


def test_duplicate_scenario_is_rejected(scoring_inputs):
    assessment, rows = scoring_inputs
    rows[1] = rows[0].copy()
    save_rows(assessment, rows)
    assert errors(validator.validate_human_scores(assessment, now=NOW)) == ['duplicate_scenario_id']


@pytest.mark.parametrize('damage', ['missing_row', 'extra_row', 'missing_column', 'extra_column', 'extra_header',
                                    'duplicate_header', 'blank_row', 'hidden_reviewer', 'hidden_header'])
def test_csv_structure_has_no_silent_missing_extra_or_hidden_data(scoring_inputs, damage):
    assessment, rows = scoring_inputs
    header = list(validator.COLUMNS)
    if damage == 'missing_row':
        rows.pop()
    elif damage == 'extra_row':
        rows.append(rows[0].copy())
    elif damage == 'missing_column':
        rows[0] = list(rows[0].values())[:-1]
    elif damage == 'extra_column':
        rows[0] = [*rows[0].values(), '']
    elif damage == 'extra_header':
        header.append('hidden_extra')
    elif damage == 'duplicate_header':
        header[-1] = 'comments'
    elif damage == 'blank_row':
        rows.append([])
    elif damage == 'hidden_header':
        header[0] += '\u200b'
    else:
        complete(rows[0])
        rows[0]['reviewer'] = '评审\u200b甲'
    save_rows(assessment, rows, header=header)
    assert not validator.validate_human_scores(assessment, now=NOW)['valid']


@pytest.mark.parametrize('change,expected', [
    ({'attribution_score_0_to_5': '0'}, 'score_pair_incomplete'),
    ({'reviewer': '合成评审甲'}, 'pending_row_contains_review_metadata'),
    ({'comments': COMMENT}, 'pending_row_contains_review_metadata'),
    ({'reviewed_at': REVIEWED}, 'pending_row_contains_review_metadata'),
])
def test_half_filled_pending_row_is_explicitly_rejected(scoring_inputs, change, expected):
    assessment, rows = scoring_inputs
    rows[0].update(change)
    save_rows(assessment, rows)
    assert errors(validator.validate_human_scores(assessment, now=NOW)) == [expected]


@pytest.mark.parametrize('field,value,expected', [
    ('reviewer', '', 'named_reviewer_required'),
    ('reviewer', '匿名', 'named_reviewer_required'),
    ('reviewer', 'TBD', 'named_reviewer_required'),
    ('reviewed_at', '', 'reviewed_at_requires_iso8601_timezone'),
    ('reviewed_at', '2026-09-19T09:30:00', 'reviewed_at_requires_iso8601_timezone'),
    ('reviewed_at', '2026-13-19T09:30:00Z', 'reviewed_at_invalid'),
    ('reviewed_at', '2026-09-19T10:00:01Z', 'reviewed_at_in_future'),
    ('reviewed_at', '2027-01-01T00:00:00+08:00', 'reviewed_at_in_future'),
    ('comments', '', 'specific_comment_required_min_8_characters'),
    ('comments', 'ok', 'specific_comment_required_min_8_characters'),
    ('comments', '123456789', 'specific_comment_required_min_8_characters'),
])
def test_complete_scores_require_named_timestamped_specific_review(scoring_inputs, field, value, expected):
    assessment, rows = scoring_inputs
    complete(rows[0])
    rows[0][field] = value
    save_rows(assessment, rows)
    assert errors(validator.validate_human_scores(assessment, now=NOW)) == [expected]


def test_cli_pending_codes_output_privacy_and_no_input_overwrite(scoring_inputs, capsys):
    assessment, rows = scoring_inputs
    before = {path.name: path.read_bytes() for path in assessment.parent.iterdir()}
    assert validator.main(['--assessment', str(assessment)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'pending'
    output = assessment.parent / 'ratings-pending.json'
    args = ['--assessment', str(assessment), '--require-complete', '--output', str(output)]
    assert validator.main(args) == 2
    summary = json.loads(capsys.readouterr().out)
    stored = json.loads(output.read_text(encoding='utf-8'))
    assert summary['exit_code'] == stored['exit_code'] == 2
    assert stored['completed_scenarios'] == 0 and stored['scenarios'][0]['reviewer'] is None
    original_output = output.read_bytes()
    assert validator.main(args) == 1
    capsys.readouterr()
    assert output.read_bytes() == original_output
    assert validator.main(['--assessment', str(assessment), '--output', str(assessment)]) == 1
    capsys.readouterr()
    assert validator.main(['--assessment', str(assessment), '--output', str(assessment.parent / 'human_scores.csv')]) == 1
    capsys.readouterr()
    assert all((assessment.parent / name).read_bytes() == raw for name, raw in before.items())
    assert not list(assessment.parent.glob('.human-scores-*.tmp'))


def test_cli_complete_and_invalid_codes(scoring_inputs, capsys):
    assessment, rows = scoring_inputs
    for row in rows:
        complete(row)
        row['reviewed_at'] = '2020-01-01T00:00:00Z'  # Always past; CLI uses the real clock.
    save_rows(assessment, rows)
    assert validator.main(['--assessment', str(assessment), '--require-complete']) == 0
    stdout = capsys.readouterr().out
    assert json.loads(stdout)['status'] == 'human_ratings_recorded'
    assert '合成测试评审甲' not in stdout and COMMENT not in stdout
    rows[0]['benchmark_score_0_to_5'] = 'NaN'
    save_rows(assessment, rows)
    output = assessment.parent / 'ratings-invalid.json'
    assert validator.main(['--assessment', str(assessment), '--require-complete', '--output', str(output)]) == 1
    capsys.readouterr()
    assert json.loads(output.read_text(encoding='utf-8'))['valid'] is False


def test_atomic_publication_loses_race_without_replacing_history(scoring_inputs, monkeypatch):
    assessment, _ = scoring_inputs
    output = assessment.parent / 'history.json'
    original_link = validator.os.link
    def publish_after_competing_writer(source, destination):
        # The race occurs AFTER the preflight existence check. The competing file
        # must survive intact even when publication attempts the same destination.
        Path(destination).write_bytes(b'previous-review-history')
        return original_link(source, destination)
    monkeypatch.setattr(validator.os, 'link', publish_after_competing_writer)
    with pytest.raises(FileExistsError):
        validator._atomic_new_json(output, {'valid': True}, inputs=(assessment,))
    assert output.read_bytes() == b'previous-review-history'
    assert not list(assessment.parent.glob('.human-scores-*.tmp'))


@pytest.mark.parametrize('damage', ['invalid_json', 'oversized_json_integer', 'duplicate_json_key', 'invalid_hash', 'duplicate_scenario', 'missing_csv', 'invalid_utf8'])
def test_bad_assessment_or_csv_input_returns_invalid(scoring_inputs, damage):
    assessment, _ = scoring_inputs
    if damage == 'invalid_json':
        assessment.write_text('{', encoding='utf-8')
    elif damage == 'oversized_json_integer':
        assessment.write_text('{"number":' + '1' * 5000 + '}', encoding='utf-8')
    elif damage == 'duplicate_json_key':
        assessment.write_text('{"schema_version":1,"schema_version":2}', encoding='utf-8')
    elif damage in ('invalid_hash', 'duplicate_scenario'):
        document = json.loads(assessment.read_text(encoding='utf-8'))
        if damage == 'invalid_hash':
            document['scenarios'][0]['frozen_hash'] = 'unbound'
        else:
            document['scenarios'][1] = document['scenarios'][0].copy()
        assessment.write_text(json.dumps(document), encoding='utf-8')
    elif damage == 'missing_csv':
        (assessment.parent / 'human_scores.csv').unlink()
    else:
        (assessment.parent / 'human_scores.csv').write_bytes(b'\xff\xff')
    report = validator.validate_human_scores(assessment, now=NOW)
    assert not report['valid'] and report['errors']

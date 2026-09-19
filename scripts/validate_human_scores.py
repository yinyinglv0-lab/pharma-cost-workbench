#!/usr/bin/env python3
"""Validate the sibling human_scores.csv against one acceptance assessment.

This CLI records named ratings; it does not authenticate reviewers or judge their
opinions. It never generates scores, updates inputs, calls a model or uses a network.
Exactly three scenario rows and the ten template columns are required (UTF-8 BOM
supported). Identity columns match the assessment exactly. A pending row has both
scores and reviewer/time/comments empty. Partial entries are rejected. A complete
row has two finite decimal scores in [0, 5], a non-placeholder reviewer, an ISO8601
time with timezone no later than validation, and a specific comment (at least eight
non-whitespace characters, not a placeholder). The CSV status column is advisory;
completion is derived from the scores and review metadata.

Exit 0: valid, including pending; exit 2: valid pending with --require-complete;
exit 1: invalid data or output failure. --output publishes a NEW private JSON file
atomically without replacing any file. Its parent directory must already exist.
Stdout contains a summary without reviewer names or comments. Scores/averages in
JSON are decimal strings (averages rounded half-up to six decimal places); empty
scores and averages with no completed reviews are null, never zero.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

COLUMNS = ('scenario_id', 'product', 'analysis_theme', 'report_id',
           'attribution_score_0_to_5', 'benchmark_score_0_to_5',
           'reviewer', 'reviewed_at', 'comments', 'status')
SCORE_FIELDS = COLUMNS[4:6]
MAX_ASSESSMENT_BYTES = 8 * 1024 * 1024
MAX_CSV_BYTES = 1024 * 1024
MIN_COMMENT_CHARACTERS = 8
PLACEHOLDERS = frozenset({'n/a', 'na', 'none', 'null', 'tbd', 'todo', 'pending',
    'anonymous', 'reviewer', '-', '--', '...', '匿名', '未知', '未填写', '待填写',
    '待评分', '待审核', '待人工审核', '评审人', '审核人', '无'})
GENERIC_COMMENTS = PLACEHOLDERS | {'ok', 'good', 'done', 'approved', '通过', '好', '很好', '已审核'}


class ScoreValidationError(ValueError):
    def __init__(self, code, *, row=None, field=None):
        super().__init__(code)
        self.code, self.row, self.field = code, row, field

    def as_dict(self):
        result = {'code': self.code}
        if self.row is not None:
            result['csv_record'] = self.row
        if self.field is not None:
            result['field'] = self.field
        return result


def _require(condition, code, *, row=None, field=None):
    if not condition:
        raise ScoreValidationError(code, row=row, field=field)


def _read_bounded(path, maximum):
    _require(path.is_file(), 'input_file_missing')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    _require(len(raw) <= maximum, 'input_too_large')
    return raw


def _input_description(raw):
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def _visible(text, *, multiline=False):
    if not isinstance(text, str):
        return False
    return all(not unicodedata.category(char).startswith('C')
               or (multiline and char in '\r\n') for char in text)


def _plain_text(value, limit, code):
    _require(isinstance(value, str) and bool(value.strip()) and len(value) <= limit
             and _visible(value), code)
    return value


def _unique_members(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'assessment_duplicate_json_key')
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ScoreValidationError('assessment_nonfinite_json_number')


def _assessment(raw):
    try:
        data = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_members,
                          parse_constant=_reject_json_constant)
    except ScoreValidationError:
        raise
    except (ValueError, RecursionError):
        raise ScoreValidationError('assessment_invalid_json') from None
    _require(isinstance(data, dict) and data.get('schema_version') == 'acceptance-scenarios/1.0',
             'unsupported_assessment')
    _plain_text(data.get('run_id'), 200, 'assessment_run_id_missing')
    scenarios = data.get('scenarios')
    _require(isinstance(scenarios, list) and len(scenarios) == 3, 'assessment_requires_three_scenarios')
    ids, reports = set(), set()
    for scenario in scenarios:
        _require(isinstance(scenario, dict), 'assessment_invalid_scenario')
        ident = _plain_text(scenario.get('scenario_id'), 80, 'assessment_scenario_id_invalid')
        _require(re.fullmatch(r'[A-Za-z0-9_-]+', ident) is not None, 'assessment_scenario_id_invalid')
        report_id = _plain_text(scenario.get('report_id'), 120, 'assessment_report_id_invalid')
        _require(ident not in ids and report_id not in reports, 'assessment_duplicate_scenario_or_report')
        ids.add(ident)
        reports.add(report_id)
        _plain_text(scenario.get('product'), 250, 'assessment_product_missing')
        _plain_text(scenario.get('theme'), 250, 'assessment_theme_missing')
        _require(isinstance(scenario.get('frozen_hash'), str)
                 and re.fullmatch('[0-9a-f]{64}', scenario['frozen_hash']) is not None,
                 'assessment_frozen_hash_invalid')
    model = data.get('model_used', {})
    _require(isinstance(model, dict), 'assessment_model_summary_invalid')
    for key in ('used_count', 'attempted_modules'):
        if key in model:
            _require(type(model[key]) is int and model[key] >= 0, 'assessment_model_summary_invalid')
    if 'all_requested_validated' in model:
        _require(type(model['all_requested_validated']) is bool, 'assessment_model_summary_invalid')
    if 'status' in model:
        _plain_text(model['status'], 100, 'assessment_model_summary_invalid')
    return data


def _csv_rows(raw):
    try:
        text = raw.decode('utf-8-sig')
        records = list(csv.reader(io.StringIO(text, newline=''), strict=True))
    except (UnicodeDecodeError, csv.Error):
        raise ScoreValidationError('scores_csv_invalid_utf8_or_syntax') from None
    _require(bool(records) and tuple(records[0]) == COLUMNS, 'scores_csv_header_mismatch')
    _require(len(records) == 4, 'scores_csv_requires_exactly_three_rows')
    rows = []
    for number, record in enumerate(records[1:], start=2):
        _require(len(record) == len(COLUMNS), 'scores_csv_column_count_mismatch', row=number)
        row = dict(zip(COLUMNS, record))
        for field, value in row.items():
            _require(len(value) <= (4000 if field == 'comments' else 512),
                     'scores_csv_field_too_long', row=number, field=field)
            _require(_visible(value, multiline=(field == 'comments')),
                     'scores_csv_hidden_or_control_character', row=number, field=field)
        rows.append((number, row))
    return rows


def _decimal_score(text, *, row, field):
    _require(len(text) <= 32, 'score_too_long', row=row, field=field)
    try:
        score = Decimal(text)
    except InvalidOperation:
        raise ScoreValidationError('score_not_decimal', row=row, field=field) from None
    _require(score.is_finite(), 'score_not_finite', row=row, field=field)
    _require(re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', text) is not None,
             'score_requires_plain_decimal', row=row, field=field)
    _require(Decimal(0) <= score <= Decimal(5), 'score_out_of_range', row=row, field=field)
    return score


def _decimal_text(score):
    if score == 0:
        return '0'
    return format(score, 'f').rstrip('0').rstrip('.') if '.' in format(score, 'f') else format(score, 'f')


def _review_time(text, now, *, row):
    _require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})', text) is not None,
             'reviewed_at_requires_iso8601_timezone', row=row, field='reviewed_at')
    try:
        instant = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        raise ScoreValidationError('reviewed_at_invalid', row=row, field='reviewed_at') from None
    _require(instant.utcoffset() is not None, 'reviewed_at_requires_iso8601_timezone', row=row, field='reviewed_at')
    _require(instant <= now, 'reviewed_at_in_future', row=row, field='reviewed_at')
    return instant.astimezone(timezone.utc).isoformat()


def _review(row, number, scenario, now):
    fields = {key: row[key].strip() for key in COLUMNS[4:]}
    filled = [bool(fields[key]) for key in SCORE_FIELDS]
    _require(filled[0] == filled[1], 'score_pair_incomplete', row=number)
    result = {'scenario_id': scenario['scenario_id'], 'product': scenario['product'],
              'analysis_theme': scenario['theme'], 'report_id': scenario['report_id'],
              'report_frozen_hash': scenario['frozen_hash'],
              'attribution_score_0_to_5': None, 'benchmark_score_0_to_5': None,
              'reviewer': None, 'reviewed_at': None, 'reviewed_at_utc': None,
              'comments': None, 'status': 'pending', 'identity_verified': False}
    if not filled[0]:
        _require(not any(fields[key] for key in ('reviewer', 'reviewed_at', 'comments')),
                 'pending_row_contains_review_metadata', row=number)
        return result, None
    scores = [_decimal_score(fields[key], row=number, field=key) for key in SCORE_FIELDS]
    reviewer = fields['reviewer']
    _require(bool(reviewer) and reviewer.casefold() not in PLACEHOLDERS,
             'named_reviewer_required', row=number, field='reviewer')
    instant = _review_time(fields['reviewed_at'], now, row=number)
    comment = fields['comments']
    _require(comment.casefold() not in GENERIC_COMMENTS
             and sum(not char.isspace() for char in comment) >= MIN_COMMENT_CHARACTERS
             and any(char.isalpha() for char in comment),
             'specific_comment_required_min_8_characters', row=number, field='comments')
    result.update({key: _decimal_text(score) for key, score in zip(SCORE_FIELDS, scores)})
    result.update(reviewer=reviewer, reviewed_at=fields['reviewed_at'], reviewed_at_utc=instant,
                  comments=comment, status='human_ratings_recorded')
    return result, scores


def _average(values):
    if not values:
        return None
    with localcontext() as context:
        context.prec = 64
        result = (sum(values, Decimal(0)) / Decimal(len(values))).quantize(Decimal('.000001'), rounding=ROUND_HALF_UP)
    return _decimal_text(result)


def validate_human_scores(assessment, *, now=None):
    """Read input snapshots only. ``now`` is injectable for deterministic tests."""
    now = now if now is not None else datetime.now(timezone.utc)
    _require(isinstance(now, datetime) and now.utcoffset() is not None, 'validation_clock_requires_timezone')
    assessment = Path(assessment)
    scores_path = assessment.parent / 'human_scores.csv'
    result = {'schema_version': 'human-scores-validation/1.0', 'validated_at': now.astimezone(timezone.utc).isoformat(),
        'valid': False, 'complete': False, 'status': 'invalid_human_scores', 'inputs': {},
        'run_id': None, 'scenarios': [], 'errors': [], 'completed_scenarios': None, 'expected_scenarios': 3,
        'pending_scenarios': None, 'completion_fraction': None, 'score_values_recorded': None,
        'averages': {key: None for key in (*SCORE_FIELDS, 'all_scores_0_to_5')},
        'averages_include': 'complete_scenarios_only', 'averages_decimal_places': 6,
        'decimal_encoding': 'strings; null means unscored', 'csv_status_role': 'advisory_only',
        'identity_verification': 'not_verified', 'rating_authenticity_verified': False,
        'inputs_modified': False, 'assessment_model_result_changed': False,
        'network_requests': 0, 'model_requests': 0, 'rpa_requests': 0}
    try:
        raw_assessment = _read_bounded(assessment, MAX_ASSESSMENT_BYTES)
        result['inputs']['assessment'] = _input_description(raw_assessment)
        raw_csv = _read_bounded(scores_path, MAX_CSV_BYTES)
        result['inputs']['human_scores_csv'] = _input_description(raw_csv)
        document = _assessment(raw_assessment)
        result['run_id'] = document['run_id']
        result['assessment_model_result'] = {key: document.get('model_used', {})[key]
            for key in ('used_count', 'attempted_modules', 'all_requested_validated', 'status')
            if key in document.get('model_used', {})}
        records = _csv_rows(raw_csv)
        scenarios = {row['scenario_id']: row for row in document['scenarios']}
        by_id = {}
        for number, row in records:
            ident = row['scenario_id']
            _require(ident in scenarios, 'scenario_id_not_in_assessment', row=number, field='scenario_id')
            _require(ident not in by_id, 'duplicate_scenario_id', row=number, field='scenario_id')
            for column, field in (('product', 'product'), ('analysis_theme', 'theme'), ('report_id', 'report_id')):
                _require(row[column] == scenarios[ident][field], 'scenario_identity_mismatch', row=number, field=column)
            by_id[ident] = number, row
        _require(set(by_id) == set(scenarios), 'assessment_scenario_set_mismatch')
        verified, complete_scores = [], []
        for scenario in document['scenarios']:
            number, row = by_id[scenario['scenario_id']]
            record, scores = _review(row, number, scenario, now)
            verified.append(record)
            if scores is not None:
                complete_scores.append(scores)
        count = len(complete_scores)
        result.update(valid=True, complete=(count == 3), status='human_ratings_recorded' if count == 3 else 'pending',
            scenarios=verified, completed_scenarios=count, pending_scenarios=3 - count,
            completion_fraction=f'{count}/3', score_values_recorded=2 * count)
        result['averages'] = {key: _average([scores[index] for scores in complete_scores]) for index, key in enumerate(SCORE_FIELDS)}
        result['averages']['all_scores_0_to_5'] = _average([score for scores in complete_scores for score in scores])
    except ScoreValidationError as exc:
        result['errors'].append(exc.as_dict())
    except (OSError, RecursionError) as exc:
        result['errors'].append({'code': 'input_read_failed', 'error_type': type(exc).__name__})
    return result


def _atomic_new_json(output, value, *, inputs):
    output = Path(output).absolute()
    _require(output.suffix.lower() == '.json', 'output_requires_json_extension')
    _require(output.resolve() not in {Path(path).resolve() for path in inputs}, 'output_is_input')
    _require(not output.exists() and not output.is_symlink(), 'output_already_exists')
    _require(output.parent.is_dir(), 'output_parent_missing')
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    descriptor, temporary = tempfile.mkstemp(prefix='.human-scores-', suffix='.tmp', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publication without replacement on both Windows/NTFS and POSIX.
        # Unsupported filesystems fail explicitly; never fall back to overwriting.
        os.link(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--assessment', type=Path, required=True, help='Assessment JSON; reads human_scores.csv beside it')
    parser.add_argument('--require-complete', action='store_true', help='Exit 2 when valid rows remain unscored')
    parser.add_argument('--output', type=Path, help='Optional NEW JSON file; never replaces inputs or existing history')
    args = parser.parse_args(argv)
    report = validate_human_scores(args.assessment)
    code = 1 if not report['valid'] else 2 if args.require_complete and not report['complete'] else 0
    report.update(require_complete=args.require_complete, exit_code=code)
    if args.output is not None:
        try:
            _atomic_new_json(args.output, report, inputs=(args.assessment, args.assessment.parent / 'human_scores.csv'))
        except (ScoreValidationError, OSError) as exc:
            code = 1
            report['output_error'] = exc.as_dict() if isinstance(exc, ScoreValidationError) else {
                'code': 'output_write_failed', 'error_type': type(exc).__name__}
    summary = {key: report[key] for key in ('valid', 'complete', 'status', 'run_id', 'completed_scenarios',
        'expected_scenarios', 'pending_scenarios', 'completion_fraction', 'score_values_recorded',
        'averages', 'identity_verification', 'inputs', 'errors')}
    summary['exit_code'] = code
    if 'output_error' in report:
        summary['output_error'] = report['output_error']
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())

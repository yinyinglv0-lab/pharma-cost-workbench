#!/usr/bin/env python3
"""Offline administration CLI. A real backup is only run by an operator."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from enterprise.operations import (OperationsError, acknowledge_restore_review,
    clear_maintenance, create_backup, operations_metrics, restore_backup, verify_backup)


def parser():
    command = argparse.ArgumentParser(description='Single-instance consistent backup, verification and isolated restore')
    command.add_argument('--data-root', type=Path, help='Default: COST_DATA_DIR')
    command.add_argument('--managed-root', type=Path, help='Default: COST_MANAGED_DIR')
    actions = command.add_subparsers(dest='action', required=True)
    backup = actions.add_parser('backup', help='Requires UI/API, workers, watchers and ingestion stopped')
    backup.add_argument('--output', type=Path, required=True)
    backup.add_argument('--writers-stopped', action='store_true', required=True,
                        help='Assert that all writing services and scripts have been stopped')
    for name in ('verify', 'restore'):
        sub = actions.add_parser(name)
        sub.add_argument('--archive', type=Path, required=True)
        sub.add_argument('--sha256-file', type=Path, help='Default: ARCHIVE.sha256; store a trusted copy separately')
        sub.add_argument('--max-unpacked-gib', type=int, default=50)
        if name == 'restore':
            sub.add_argument('--target', type=Path, required=True, help='New/empty parent for data/ and managed/')
    actions.add_parser('metrics', help='Read-only aggregate operational metrics; no payloads')
    clear = actions.add_parser('maintenance-clear', help='Clear an abandoned maintenance marker after investigation')
    clear.add_argument('--writers-stopped', action='store_true', required=True)
    review = actions.add_parser('review-complete', help='Record completed external receipt reconciliation; no task approval or send')
    review.add_argument('--writers-stopped', action='store_true', required=True)
    review.add_argument('--actor', required=True)
    review.add_argument('--note', required=True, help='Reconciliation ticket/reference; do not include secrets')
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == 'backup':
            result = create_backup(args.output, data_root=args.data_root, managed_root=args.managed_root,
                                   writers_stopped=args.writers_stopped)
        elif args.action in ('verify', 'restore'):
            sidecar = args.sha256_file or args.archive.with_name(args.archive.name + '.sha256')
            expected = sidecar.read_text(encoding='ascii').strip()
            if args.max_unpacked_gib < 1:
                raise OperationsError('Uncompressed archive size limit must be positive.')
            options = {'expected_sha256': expected, 'max_bytes': args.max_unpacked_gib * 1024 ** 3}
            if args.action == 'restore':
                result = restore_backup(args.archive, args.target, **options)
            else:
                manifest = verify_backup(args.archive, **options)
                result = {'verified': True, 'backup_id': manifest['backup_id'], 'files': len(manifest['entries'])}
        elif args.action == 'maintenance-clear':
            clear_maintenance(args.managed_root, writers_stopped=args.writers_stopped)
            result = {'maintenance_marker_cleared': True}
        elif args.action == 'review-complete':
            result = acknowledge_restore_review(args.managed_root, actor=args.actor, note=args.note,
                                                writers_stopped=args.writers_stopped)
        else:
            result = operations_metrics(data_root=args.data_root, managed_root=args.managed_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except OperationsError as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
    except (OSError, ValueError) as exc:
        print(json.dumps({'ok': False, 'error_type': type(exc).__name__,
                          'error': 'Operation failed; check paths, disk, access and checksum file.'}), file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())

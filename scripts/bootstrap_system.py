"""Explicit source registration and versioned knowledge publication."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _build_timeout(value):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError('build timeout must be seconds in (0, 3600]') from None
    if not math.isfinite(seconds) or not 0 < seconds <= 3600:
        raise argparse.ArgumentTypeError('build timeout must be seconds in (0, 3600]')
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description='Register reviewed official knowledge sources')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--root', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dense', dest='dense', action='store_true', default=True,
                      help='Require local BGE embeddings (default; contest compliant hybrid)')
    mode.add_argument('--lexical-diagnostic-only', dest='dense', action='store_false',
                      help='Explicit noncompliant diagnostic release; formal model analysis will refuse it')
    parser.add_argument('--catalog-only', action='store_true')
    parser.add_argument('--build-timeout', type=_build_timeout, default=600.0,
                        help='Explicit offline publication deadline in seconds (0 < seconds <= 3600; default: 600)')
    args = parser.parse_args(argv)
    from enterprise.security import local_principal, auth_mode
    if auth_mode() != 'local':
        parser.error('Production registration must use the authenticated knowledge administration UI')
    from enterprise.bootstrap import bootstrap_knowledge
    result = bootstrap_knowledge(principal=local_principal(), source_root=args.source, root=args.root,
                                 publish=not args.catalog_only, dense=args.dense, build_timeout=args.build_timeout)
    release = result.get('release') or {}
    print(json.dumps({'status': result['status'], 'documents': len(result['versions']),
                      'release_id': release.get('release_id'),
                      'chunk_count': release.get('manifest', {}).get('chunk_count'),
                      'embedding': release.get('manifest', {}).get('embedding'),
                      'degraded': release.get('manifest', {}).get('degraded'), 'error': release.get('error')},
                     ensure_ascii=False, indent=2, default=str))
    return 1 if result['status'] == 'failed' or release.get('status') == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())

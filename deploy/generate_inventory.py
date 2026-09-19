#!/usr/bin/env python3
"""Generate a factual component/source/hash inventory; not a license approval.

No network requests, model loading, business-file traversal or secret-file reads.
Run this in the exact delivery environment, then archive the output with its image
hash, model acquisition records, notices and the human redistribution decision.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys

ROOT = Path(__file__).resolve().parents[1]
CODE_TREES = ('enterprise', 'app_pages', 'dashboard', 'report', 'rag_fixed_v1', 'scripts', 'deploy')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def inventory(*, model_roots=(), source_revision='unrecorded'):
    packages = []
    for distribution in importlib.metadata.distributions():
        metadata = distribution.metadata
        urls = metadata.get_all('Project-URL') or []
        packages.append({'name': metadata.get('Name', 'unknown'), 'version': distribution.version,
            'license_expression': metadata.get('License-Expression'),
            'license_metadata': (metadata.get('License') or '')[:1000],
            'license_classifiers': [x for x in (metadata.get_all('Classifier') or []) if x.startswith('License ::')],
            'source_references': urls, 'homepage': metadata.get('Home-page'),
            'redistribution_decision': 'not_reviewed'})
    paths = list(ROOT.glob('*.py')) + [ROOT / 'requirements.txt', ROOT / 'requirements-models.txt',
                                        ROOT / 'Dockerfile', ROOT / 'compose.yaml', ROOT / '.dockerignore']
    for tree in CODE_TREES:
        paths.extend((ROOT / tree).rglob('*.py'))
    paths.append(ROOT / 'assets/echarts.min.js')
    files = []
    for path in sorted(set(paths)):
        if not path.is_file() or path.is_symlink() or '__pycache__' in path.parts:
            continue
        files.append({'path': path.relative_to(ROOT).as_posix(), 'sha256': digest(path), 'bytes': path.stat().st_size})
    models = []
    for argument in model_roots:
        root = Path(argument).resolve()
        if not root.is_dir():
            raise ValueError('Model root must be an existing directory.')
        entries = []
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise ValueError('Model inventories require materialized files, not symlinks.')
            if path.is_file():
                entries.append({'path': path.relative_to(root).as_posix(), 'sha256': digest(path), 'bytes': path.stat().st_size})
        models.append({'name': root.name, 'files': entries,
                       'source_revision': 'record_exact_upstream_revision',
                       'license_and_redistribution_decision': 'not_reviewed'})
    return {'schema': 'project4-component-inventory-v1',
            'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'scope': 'installed Python environment plus allowlisted source; system libraries/fonts need image package inventory',
            'python': platform.python_version(), 'platform': platform.platform(),
            'source_revision': source_revision, 'files': files,
            'packages': sorted(packages, key=lambda entry: entry['name'].lower()), 'models': models,
            'additional_components': [
                {'name': 'Apache ECharts', 'path': 'assets/echarts.min.js',
                 'upstream': 'https://echarts.apache.org/', 'decision': 'verify bundled version, license and NOTICE'},
                {'name': 'Noto CJK fonts', 'source': 'Debian fonts-noto-cjk in Dockerfile',
                 'upstream': 'https://github.com/notofonts/noto-cjk', 'decision': 'archive installed package copyright and license'},
                {'name': 'WenQuanYi Zen Hei', 'source': 'Debian fonts-wqy-zenhei in Dockerfile',
                 'upstream': 'https://wenq.org/', 'decision': 'archive font license and embedding/redistribution terms'},
                {'name': 'Python base image and OS packages', 'source': 'Dockerfile',
                 'decision': 'archive image digest and OS package/notice inventory'}],
            'warning': 'Metadata and hashes are not a complete SPDX/CycloneDX SBOM, provenance attestation, vulnerability scan or license authorization.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model-root', action='append', default=[])
    parser.add_argument('--source-revision', default='unrecorded')
    args = parser.parse_args(argv)
    try:
        result = inventory(model_roots=args.model_root, source_revision=args.source_revision)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    except (OSError, ValueError) as exc:
        print('Inventory failed: ' + type(exc).__name__ + '; check roots and use a new output filename.', file=sys.stderr)
        return 1
    print('Inventory created; redistribution decisions remain pending human review.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

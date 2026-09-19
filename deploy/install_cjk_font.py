#!/usr/bin/env python3
"""Install one unmodified, hash-pinned OFL TrueType font during image build.

Only the fixed upstream font and its matching license are fetched. This script
is never invoked by the application or at container startup. No model weights,
Windows fonts, business files or credentials are inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

UPSTREAM_COMMIT = 'a85815a42757630ce188fdad368c2dfc444d4773'
UPSTREAM_ROOT = 'https://raw.githubusercontent.com/google/fonts/' + UPSTREAM_COMMIT + '/ofl/notosanssc/'
RESOURCES = (
    {'file': 'NotoSansSC-VF.ttf', 'upstream_file': 'NotoSansSC%5Bwght%5D.ttf',
     'bytes': 17772300, 'sha256': 'a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da',
     'kind': 'font'},
    {'file': 'NotoSansSC-OFL.txt', 'upstream_file': 'OFL.txt',
     'bytes': 4388, 'sha256': '1c05c68c34f9708415aada51f17e1b0092d2cea709bf4a94cd38114f9e73d7d9',
     'kind': 'notice'},
)


def fetch_verified(resource):
    url = UPSTREAM_ROOT + resource['upstream_file']
    request = urllib.request.Request(url, headers={'User-Agent': 'project4-font-build/1.0'})
    with urllib.request.urlopen(request, timeout=90) as response:
        if not response.geturl().startswith('https://'):
            raise ValueError('font_download_requires_https')
        raw = response.read(resource['bytes'] + 1)
    if len(raw) != resource['bytes'] or hashlib.sha256(raw).hexdigest() != resource['sha256']:
        raise ValueError('font_resource_hash_or_size_mismatch:' + resource['file'])
    if resource['kind'] == 'font' and not raw.startswith(b'\x00\x01\x00\x00'):
        raise ValueError('font_resource_must_have_truetype_outlines')
    return raw


def atomic_install(destination, raw):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, name = tempfile.mkstemp(prefix='.font-build-', dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
        temporary.chmod(0o644)  # Image is built as root but used by UID 10001.
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font-dir', type=Path, default=Path('/usr/share/fonts/truetype/noto'))
    parser.add_argument('--notice-dir', type=Path, default=Path('/usr/share/doc/project4-fonts'))
    args = parser.parse_args(argv)
    try:
        # Verify both resources before placing either in the final directories.
        fetched = [(resource, fetch_verified(resource)) for resource in RESOURCES]
        for resource, raw in fetched:
            directory = args.font_dir if resource['kind'] == 'font' else args.notice_dir
            atomic_install(directory / resource['file'], raw)
        manifest = {'schema_version': 'project4-build-font/1.0', 'family': 'Noto Sans SC',
                    'license': 'SIL Open Font License 1.1', 'upstream_commit': UPSTREAM_COMMIT,
                    'upstream_bytes_unmodified': True, 'variable_font_default_weight': 100,
                    'resources': [{'file': resource['file'], 'source': UPSTREAM_ROOT + resource['upstream_file'],
                                   'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
                                  for resource, raw in fetched]}
        atomic_install(args.notice_dir / 'FONT-MANIFEST.json',
                       (json.dumps(manifest, ensure_ascii=True, indent=2) + '\n').encode('utf-8'))
        print(json.dumps({'status': 'installed', **manifest}, ensure_ascii=True))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'stage': 'build_font_install',
                          'error_type': type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

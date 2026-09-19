"""Source identity for managed launcher reuse; excludes data, secrets and tests."""
from hashlib import sha256
from paths import BASE_DIR


def source_fingerprint():
    paths = list(BASE_DIR.glob('*.py'))
    for directory in ('enterprise', 'dashboard', 'app_pages', 'report'):
        paths.extend((BASE_DIR / directory).rglob('*.py'))
    digest = sha256()
    for path in sorted(paths):
        if path.name.startswith('test_') or '__pycache__' in path.parts:
            continue
        digest.update(path.relative_to(BASE_DIR).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()

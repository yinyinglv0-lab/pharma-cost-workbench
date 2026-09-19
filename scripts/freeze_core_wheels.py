"""Freeze the installed core dependency closure into an audited offline wheel set.

Run with the working Python 3.12 Windows environment. Installed distribution
metadata supplies versions and dependency declarations only; package files are
NEVER copied into the isolated target. Wheels come exclusively from official
PyPI metadata and files.pythonhosted.org, and every byte is SHA256 checked.

    .venv\\Scripts\\python.exe scripts\\freeze_core_wheels.py

The default run also installs the hash-locked closure in .runtime/clean-venv
using --no-index, --require-hashes, --only-binary and a Python -I invocation,
then checks package locations, exact versions, extras closure and pip check.
"""
from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import http.client
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_HOSTS = frozenset({'pypi.org', 'files.pythonhosted.org'})
MAX_RETRIES = 2
READ_TIMEOUT = 30
TOTAL_DOWNLOAD_TIMEOUT = 300
CHUNK_BYTES = 1024 * 1024


class FreezeError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK_BYTES), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.writing')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in OFFICIAL_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment):
        raise FreezeError('Only HTTPS pypi.org and files.pythonhosted.org URLs are permitted')
    return url


class OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_official(url, *, extra_headers=None):
    validate_url(url)
    opener = urllib.request.build_opener(OfficialRedirectHandler())
    request = urllib.request.Request(url, headers={'User-Agent': 'project4-core-wheel-freezer/1.0',
                                                  'Accept': 'application/json, application/octet-stream',
                                                  **(extra_headers or {})})
    response = opener.open(request, timeout=READ_TIMEOUT)
    validate_url(response.geturl())
    return response


def _transient(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 429, 500, 502, 503, 504}
    if isinstance(exc, urllib.error.URLError):
        # An operating-system permission denial is not a network retry condition.
        if isinstance(exc.reason, PermissionError):
            return False
        return True
    return isinstance(exc, (http.client.IncompleteRead, http.client.RemoteDisconnected,
                            TimeoutError, socket.timeout, ConnectionResetError,
                            ConnectionAbortedError, json.JSONDecodeError))


def with_network_retries(operation, *, label):
    """At most initial attempt + two retries of the SAME URL operation."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            return operation(attempt + 1)
        except Exception as exc:
            if not _transient(exc) or attempt == MAX_RETRIES:
                raise
            print(f'network retry {attempt + 1}/{MAX_RETRIES}: {label} ({type(exc).__name__})', flush=True)
            time.sleep(0.5 * (attempt + 1))


def fetch_version_json(name, version):
    url = f'https://pypi.org/pypi/{urllib.parse.quote(name, safe="")}/{urllib.parse.quote(version, safe="")}/json'
    def fetch(attempt):
        with open_official(url) as response:
            body = response.read(16 * CHUNK_BYTES + 1)
            if len(body) > 16 * CHUNK_BYTES:
                raise FreezeError(f'Unexpectedly large PyPI metadata for {name}')
            declared = response.headers.get('Content-Length')
            if declared and len(body) != int(declared):
                raise http.client.IncompleteRead(body, int(declared) - len(body))
            data = json.loads(body.decode('utf-8'))
        if canonicalize_name(data.get('info', {}).get('name', '')) != name:
            raise FreezeError(f'PyPI project identity mismatch for {name}')
        if Version(data.get('info', {}).get('version', '0')) != Version(version):
            raise FreezeError(f'PyPI version mismatch for {name}=={version}')
        return data, attempt
    return with_network_retries(fetch, label=url)


def read_roots(path, environment):
    roots = []
    for number, raw in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        line = line.split(' #', 1)[0].strip()
        if line.startswith('-'):
            raise FreezeError(f'{path}:{number}: directives and external indexes are not supported')
        requirement = Requirement(line)
        if requirement.url:
            raise FreezeError(f'{path}:{number}: direct URLs are not permitted')
        if requirement.marker and not requirement.marker.evaluate({**environment, 'extra': ''}):
            continue
        pins = list(requirement.specifier)
        if len(pins) != 1 or pins[0].operator != '==' or '*' in pins[0].version:
            raise FreezeError(f'{path}:{number}: every core root must have one exact == pin')
        roots.append(requirement)
    if not roots:
        raise FreezeError('The core requirements file has no active pinned roots')
    return roots


def resolve_installed_closure(roots, environment, distribution=metadata.distribution):
    """Propagate requested extras to a fixed point, checking every active edge.

    Requires-Dist markers are evaluated against THIS target Windows/Python
    environment and every requested extra. A newly requested transitive extra
    schedules the distribution again; unrelated installed packages stay out.
    """
    nodes, declarations, queue = {}, {}, deque()
    edge_keys = set()
    edges = []

    def add(requirement, parent):
        if requirement.url:
            raise FreezeError(f'{parent}: direct dependency URLs are not permitted: {requirement.name}')
        name = canonicalize_name(requirement.name)
        if name not in nodes:
            try:
                dist = distribution(name)
            except metadata.PackageNotFoundError:
                raise FreezeError(f'Missing installed dependency: {parent} requires {requirement}') from None
            version = str(dist.version)
            actual_name = canonicalize_name(dist.metadata.get('Name', name))
            if actual_name != name:
                raise FreezeError(f'Installed distribution identity mismatch for {name}')
            nodes[name] = {'name': name, 'version': version, 'extras': set(), 'root': parent == '<root>'}
            declarations[name] = list(dist.requires or ())
            queue.append(name)
        node = nodes[name]
        if not requirement.specifier.contains(node['version'], prereleases=True):
            raise FreezeError(f'Installed version conflict: {parent} requires {requirement}; '
                              f'working environment has {name}=={node["version"]}')
        requested = {canonicalize_name(extra) for extra in requirement.extras}
        if not requested <= node['extras']:
            node['extras'].update(requested)
            queue.append(name)
        node['root'] |= parent == '<root>'
        key = (parent, str(requirement))
        if key not in edge_keys:
            edge_keys.add(key)
            edges.append({'parent': parent, 'requirement': str(requirement), 'resolved_name': name,
                          'resolved_version': node['version']})

    for root in roots:
        add(root, '<root>')
    evaluated = {}
    while queue:
        name = queue.popleft()
        extras = frozenset(nodes[name]['extras'])
        if evaluated.get(name) == extras:
            continue
        evaluated[name] = extras
        for text in declarations[name]:
            requirement = Requirement(text)
            if requirement.marker and not any(requirement.marker.evaluate({**environment, 'extra': extra})
                                              for extra in (set(extras) | {''})):
                continue
            add(requirement, name)
    for node in nodes.values():
        node['extras'] = sorted(node['extras'])
    return dict(sorted(nodes.items())), sorted(edges, key=lambda row: (row['parent'], row['requirement']))


def choose_wheel(node, data, compatible_tags):
    ranks = {tag: rank for rank, tag in enumerate(compatible_tags)}
    candidates = []
    for record in data.get('urls', []):
        filename = record.get('filename', '')
        if record.get('packagetype') != 'bdist_wheel' or Path(filename).name != filename:
            continue
        try:
            name, version, build, wheel_tags = parse_wheel_filename(filename)
        except ValueError:
            continue
        if canonicalize_name(name) != node['name'] or version != Version(node['version']):
            continue
        matches = wheel_tags & ranks.keys()
        if not matches:
            continue
        requires_python = record.get('requires_python') or data.get('info', {}).get('requires_python')
        if requires_python and not SpecifierSet(requires_python).contains(platform.python_version(), prereleases=True):
            continue
        digest = record.get('digests', {}).get('sha256', '')
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise FreezeError(f'Missing SHA256 for {filename}')
        url = validate_url(record['url'])
        if urllib.parse.urlsplit(url).hostname != 'files.pythonhosted.org':
            raise FreezeError(f'Wheel must be served by files.pythonhosted.org: {filename}')
        if Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name != filename:
            raise FreezeError(f'Wheel URL filename mismatch: {filename}')
        candidates.append((min(ranks[tag] for tag in matches), filename, {
            'filename': filename, 'url': url, 'sha256': digest, 'size': int(record.get('size') or 0),
            'compatible_tags': sorted(str(tag) for tag in matches),
            'requires_python': requires_python, 'yanked': bool(record.get('yanked')),
        }))
    if not candidates:
        raise FreezeError(f'No official wheel compatible with this Windows Python 3.12: {node["name"]}=={node["version"]}')
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def download_verified(wheel, wheelhouse):
    target = wheelhouse / wheel['filename']
    if target.is_file() and sha256_file(target) == wheel['sha256']:
        return {'download_attempts': 0, 'reused_verified_cache': True}
    partial = target.with_name(target.name + '.part')
    if wheel['size'] > CHUNK_BYTES:
        # Bounded ranges avoid long-transfer truncation. The failure/retry
        # budget belongs to the whole wheel, not separately to each range.
        started, retries, requests = time.monotonic(), 0, 0
        digest = hashlib.sha256()
        try:
            with partial.open('wb') as stream:
                for start in range(0, wheel['size'], CHUNK_BYTES):
                    end = min(start + CHUNK_BYTES, wheel['size']) - 1
                    expected = end - start + 1
                    while True:
                        if time.monotonic() - started > TOTAL_DOWNLOAD_TIMEOUT:
                            raise FreezeError('Wheel range download exceeded total deadline')
                        try:
                            requests += 1
                            with open_official(wheel['url'], extra_headers={'Range': f'bytes={start}-{end}',
                                                                          'Accept-Encoding': 'identity'}) as response:
                                if response.status != 206 or response.headers.get('Content-Range') != f'bytes {start}-{end}/{wheel["size"]}':
                                    raise FreezeError(f'Official server did not honor the exact range: {wheel["filename"]}')
                                block = response.read(expected + 1)
                            if len(block) != expected:
                                raise http.client.IncompleteRead(block, expected - len(block))
                            break
                        except Exception as exc:
                            if not _transient(exc) or retries >= MAX_RETRIES:
                                raise
                            retries += 1
                            print(f'range retry {retries}/{MAX_RETRIES}: {wheel["filename"]} bytes={start}-{end} ({type(exc).__name__})', flush=True)
                            time.sleep(0.5 * retries)
                    stream.write(block)
                    digest.update(block)
                stream.flush()
                os.fsync(stream.fileno())
            if digest.hexdigest() != wheel['sha256']:
                raise FreezeError(f'Wheel SHA256 mismatch: {wheel["filename"]}')
            os.replace(partial, target)
            return {'download_attempts': retries + 1, 'range_requests': requests,
                    'reused_verified_cache': False, 'transport': 'verified_1MiB_ranges'}
        finally:
            if partial.exists():
                partial.unlink()

    def download(attempt):
        started = time.monotonic()
        digest, count = hashlib.sha256(), 0
        try:
            with open_official(wheel['url']) as response, partial.open('wb') as stream:
                while True:
                    if time.monotonic() - started > TOTAL_DOWNLOAD_TIMEOUT:
                        raise TimeoutError('Wheel download exceeded total deadline')
                    block = response.read(CHUNK_BYTES)
                    if not block:
                        break
                    stream.write(block)
                    digest.update(block)
                    count += len(block)
                stream.flush()
                os.fsync(stream.fileno())
            if wheel['size'] and count != wheel['size']:
                raise http.client.IncompleteRead(b'', wheel['size'] - count)
            if digest.hexdigest() != wheel['sha256']:
                raise FreezeError(f'Wheel SHA256 mismatch: {wheel["filename"]}')
            os.replace(partial, target)
            return {'download_attempts': attempt, 'reused_verified_cache': False}
        finally:
            if partial.exists():
                partial.unlink()
    return with_network_retries(download, label=wheel['url'])


def fetch_wheel(node, wheelhouse, compatible_tags, cached=None):
    if cached and cached.get('name') == node['name'] and cached.get('version') == node['version']:
        filename = cached.get('filename', '')
        digest = cached.get('sha256', '')
        if Path(filename).name == filename and re.fullmatch(r'[0-9a-f]{64}', digest):
            name, version, _, wheel_tags = parse_wheel_filename(filename)
            url = validate_url(cached['url'])
            target = wheelhouse / filename
            if (canonicalize_name(name) == node['name'] and version == Version(node['version'])
                    and wheel_tags & set(compatible_tags)
                    and urllib.parse.urlsplit(url).hostname == 'files.pythonhosted.org'
                    and target.is_file() and sha256_file(target) == digest):
                print(f'reused verified {node["name"]}=={node["version"]}', flush=True)
                return {**cached, **node, 'reused_verified_cache': True, 'download_attempts': 0,
                        'cached_official_metadata': True}
    data, metadata_attempts = fetch_version_json(node['name'], node['version'])
    wheel = choose_wheel(node, data, compatible_tags)
    result = {**node, **wheel, 'metadata_attempts': metadata_attempts,
              **download_verified(wheel, wheelhouse)}
    print(f'verified {node["name"]}=={node["version"]}: {wheel["filename"]}', flush=True)
    return result


def write_lock(path, roots, wheels):
    lines = ['# Windows CPython 3.12 core closure, downloaded from official PyPI wheels.',
             '# Generated from installed versions in the working environment; hashes pin wheel bytes.',
             '# Install with --no-index --find-links .runtime/wheelhouse --require-hashes --only-binary=:all:.',
             '# Root extras (including streamlit[auth] and PyJWT[crypto]) remain explicit.']
    for wheel in sorted(wheels, key=lambda row: row['name']):
        extras = '[' + ','.join(wheel['extras']) + ']' if wheel['extras'] else ''
        lines.append(f'{wheel["name"]}{extras}=={wheel["version"]} --hash=sha256:{wheel["sha256"]}')
    Path(path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run_logged(arguments, log_path, *, timeout=600):
    """File-backed stdout/stderr avoids pipe capture and never invokes a shell."""
    with Path(log_path).open('wb') as output:
        try:
            result = subprocess.run([str(value) for value in arguments], stdout=output, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise FreezeError(f'Command exceeded {timeout}s; see {log_path}') from None
    return result.returncode


_INSPECT = r'''
import importlib.metadata as m, json, pathlib, site, sys
values=[]
for d in m.distributions():
    values.append({'name': d.metadata.get('Name'), 'version': d.version,
                   'location': str(pathlib.Path(d.locate_file('')).resolve())})
pathlib.Path(sys.argv[1]).write_text(json.dumps({'executable': sys.executable, 'prefix': sys.prefix,
    'base_prefix': sys.base_prefix, 'user_site_enabled': site.ENABLE_USER_SITE,
    'packages': sorted(values,key=lambda p:p['name'].lower())},ensure_ascii=False),encoding='utf-8')
'''


def inspect_clean(python, output, log):
    code = run_logged([python, '-I', '-B', '-c', _INSPECT, output], log, timeout=60)
    if code:
        raise FreezeError(f'Clean Python inspection failed with exit {code}; see {log}')
    return json.loads(Path(output).read_text(encoding='utf-8'))


def verify_isolation(info, expected_prefix):
    prefix = Path(info['prefix']).resolve()
    if prefix != expected_prefix.resolve() or prefix == Path(info['base_prefix']).resolve():
        raise FreezeError('Target interpreter is not the requested isolated virtual environment')
    if info['user_site_enabled']:
        raise FreezeError('Target virtual environment must disable user site packages')
    outside = [row['name'] for row in info['packages'] if not Path(row['location']).resolve().is_relative_to(prefix)]
    if outside:
        raise FreezeError('Target exposes distributions outside its prefix: ' + ', '.join(outside))


def clean_install(python, wheelhouse, lock, artifacts, nodes):
    prefix = python.parent.parent
    config = prefix / 'pyvenv.cfg'
    if not config.is_file() or not re.search(r'^include-system-site-packages\s*=\s*false\s*$',
                                            config.read_text(encoding='utf-8'), re.IGNORECASE | re.MULTILINE):
        raise FreezeError('Target pyvenv.cfg must set include-system-site-packages = false')
    before = inspect_clean(python, artifacts / 'clean_environment_before.json', artifacts / 'clean_inspect_before.log')
    verify_isolation(before, prefix)
    before_names = {canonicalize_name(row['name']) for row in before['packages']}
    unexpected = before_names - set(nodes) - {'pip', 'setuptools', 'wheel'}
    if unexpected:
        raise FreezeError('Clean target contains unrelated packages: ' + ', '.join(sorted(unexpected)))
    arguments = [python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'install',
                 '--no-index', '--find-links', wheelhouse, '--require-hashes', '--only-binary=:all:',
                 '--no-cache-dir', '--no-input', '-r', lock]
    print(f'installing {len(nodes)} locked packages in isolated target', flush=True)
    installed = run_logged(arguments, artifacts / 'clean_install_pip.log', timeout=900)
    if installed:
        raise FreezeError(f'Offline hash-locked pip install failed with exit {installed}; see .artifacts/clean_install_pip.log')
    checked = run_logged([python, '-I', '-m', 'pip', '--isolated', '--disable-pip-version-check', 'check'],
                         artifacts / 'clean_install_pip_check.log', timeout=120)
    after = inspect_clean(python, artifacts / 'clean_environment_after.json', artifacts / 'clean_inspect_after.log')
    verify_isolation(after, prefix)
    installed_versions = {canonicalize_name(row['name']): row['version'] for row in after['packages']}
    differences = {name: {'expected': node['version'], 'actual': installed_versions.get(name)}
                   for name, node in nodes.items() if installed_versions.get(name) != node['version']}
    if differences:
        raise FreezeError('Installed versions do not match the lock: ' + json.dumps(differences, ensure_ascii=False))
    extra_packages = set(installed_versions) - set(nodes) - {'pip', 'setuptools', 'wheel'}
    if extra_packages:
        raise FreezeError('Unexpected installed packages: ' + ', '.join(sorted(extra_packages)))
    if checked:
        raise FreezeError(f'pip check failed with exit {checked}; see .artifacts/clean_install_pip_check.log')
    return {'status': 'passed', 'pip_install_exit_code': installed, 'pip_check_exit_code': checked,
            'before': before, 'after': after, 'exact_versions_verified': True,
            'all_distributions_inside_clean_prefix': True, 'unrelated_packages': [],
            'pip_command': [str(value) for value in arguments],
            'pip_check_output': (artifacts / 'clean_install_pip_check.log').read_text(encoding='utf-8', errors='replace').strip()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requirements', type=Path, default=ROOT / 'requirements.txt')
    parser.add_argument('--wheelhouse', type=Path, default=ROOT / '.runtime' / 'wheelhouse')
    parser.add_argument('--lock', type=Path, default=ROOT / 'requirements-core-win-py312.lock.txt')
    parser.add_argument('--clean-python', type=Path, default=ROOT / '.runtime' / 'clean-venv' / 'Scripts' / 'python.exe')
    parser.add_argument('--workers', type=int, default=4, choices=range(1, 9))
    parser.add_argument('--download-only', action='store_true')
    args = parser.parse_args(argv)
    artifacts = ROOT / '.artifacts'
    artifacts.mkdir(parents=True, exist_ok=True)
    report_path = artifacts / 'clean_install_report.json'
    manifest_path = args.wheelhouse / 'manifest.json'
    cache = {}
    for previous_path in (report_path, manifest_path):
        if previous_path.is_file():
            previous = json.loads(previous_path.read_text(encoding='utf-8'))
            for wheel in previous.get('wheels', previous.get('verified_wheels', [])):
                cache[(wheel['name'], wheel['version'])] = wheel
    report = {'schema_version': 1, 'status': 'running', 'started_at': now(), 'source': 'official_pypi_wheels',
              'working_python': sys.executable, 'target_python': str(args.clean_python),
              'requirements': str(args.requirements), 'lock': str(args.lock), 'wheelhouse': str(args.wheelhouse),
              'download_policy': {'allowed_hosts': sorted(OFFICIAL_HOSTS), 'read_timeout_seconds': READ_TIMEOUT,
                                  'total_download_timeout_seconds': TOTAL_DOWNLOAD_TIMEOUT, 'maximum_retries': MAX_RETRIES,
                                  'large_file_range_bytes': CHUNK_BYTES, 'range_retry_budget': 'per_wheel'}, 
              'business_data_read': False, 'installed_package_files_copied': False}
    completed = []
    try:
        if sys.platform != 'win32' or sys.implementation.name != 'cpython' or sys.version_info[:2] != (3, 12):
            raise FreezeError('This lock is specifically for Windows CPython 3.12; run the working .venv interpreter')
        environment = default_environment()
        roots = read_roots(args.requirements, environment)
        nodes, edges = resolve_installed_closure(roots, environment)
        tags = list(sys_tags())
        report.update(root_requirements=[str(root) for root in roots], root_count=len(roots),
                      closure_count=len(nodes), marker_environment=environment,
                      target_primary_tag=str(tags[0]), requirements_sha256=sha256_file(args.requirements),
                      dependencies=edges)
        print(f'resolved {len(roots)} pinned roots to {len(nodes)} installed compatible distributions', flush=True)
        write_json(report_path, report)
        args.wheelhouse.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=args.workers, thread_name_prefix='official-wheel') as pool:
            futures = {pool.submit(fetch_wheel, node, args.wheelhouse, tags,
                                   cache.get((name, node['version']))): name for name, node in nodes.items()}
            try:
                for future in as_completed(futures):
                    completed.append(future.result())
            except Exception:
                for pending in futures:
                    pending.cancel()
                raise
        completed.sort(key=lambda row: row['name'])
        manifest = {'schema_version': 1, 'status': 'verified', 'created_at': now(),
                    'platform': str(tags[0]), 'root_requirements': report['root_requirements'],
                    'marker_environment': environment, 'dependencies': edges, 'wheels': completed}
        write_json(manifest_path, manifest)
        write_lock(args.lock, roots, completed)
        report.update(manifest=str(manifest_path), manifest_sha256=sha256_file(manifest_path),
                      lock_sha256=sha256_file(args.lock), wheels=completed,
                      total_wheel_bytes=sum(row['size'] for row in completed))
        if args.download_only:
            report['installation'] = {'status': 'not_requested'}
            report['status'] = 'downloaded_verified'
        else:
            report['installation'] = clean_install(args.clean_python.resolve(), args.wheelhouse.resolve(),
                                                    args.lock.resolve(), artifacts, nodes)
            report['status'] = 'passed'
        report['completed_at'] = now()
        write_json(report_path, report)
        print(json.dumps({'status': report['status'], 'roots': len(roots), 'packages': len(nodes),
                          'wheels': len(completed), 'report': str(report_path)}, ensure_ascii=True), flush=True)
        return 0
    except Exception as exc:
        report.update(status='failed', completed_at=now(), error_type=type(exc).__name__, error=str(exc),
                      verified_wheels=sorted(completed, key=lambda row: row['name']))
        write_json(report_path, report)
        print(f'freeze failed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

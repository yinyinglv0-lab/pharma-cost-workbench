#!/usr/bin/env python3
"""Liveness only: no credentials, business requests, model calls or external network."""
import urllib.error
import urllib.request


def main():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        for url in ('http://127.0.0.1:8501/_stcore/health', 'http://127.0.0.1:8000/api/health'):
            with opener.open(url, timeout=2) as response:
                if response.status != 200:
                    return 1
        return 0
    except (OSError, urllib.error.URLError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

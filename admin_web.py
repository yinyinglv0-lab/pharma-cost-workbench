"""Compatibility entry point for the authenticated, governed workbench.

The former direct Chroma/upload UI bypassed version approval and data scopes.
All historical launch commands now use the same Principal-aware navigation as
enterprise_app.py; no old mutation pipeline is reachable from this entry point.
"""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name('enterprise_app.py')), run_name='__main__')

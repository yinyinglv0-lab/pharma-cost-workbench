"""Non-blocking authorized knowledge warmup shared by Web and API startup."""
from enterprise.security import require


def readiness(principal, *, repository=None, wait=False, timeout=None, require_hybrid=True):
    """Contest readiness requires vector + BM25; lexical-only is not compliant.

    Explicit legacy/offline diagnostics can pass ``require_hybrid=False``.
    No index is published or changed by this readiness check.
    """
    require(principal,'knowledge.read')
    from enterprise.knowledge import Repository
    from enterprise.knowledge_release import get_search_engine
    repo=repository or Repository(principal=principal)
    return get_search_engine(repository=repo).readiness(
        principal=principal,wait=wait,timeout=timeout,require_hybrid=require_hybrid)


def begin_warmup(principal, *, repository=None, require_hybrid=True):
    """No network request, publication, fallback identity, or business write."""
    try:
        return readiness(principal,repository=repository,wait=False,require_hybrid=require_hybrid)
    except (ValueError,PermissionError,OSError,RuntimeError) as exc:
        # Operator status may record exception class, never paths or credentials.
        return {'ready':False,'state':'warmup_unavailable','error_type':type(exc).__name__}

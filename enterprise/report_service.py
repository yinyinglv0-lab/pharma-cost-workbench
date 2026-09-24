"""Application-facing report service; identity/authorization are supplied by APP.

The service has no repository mutations, external model client or task dispatch.
Call build_report_payload once, persist that JSON through the authorized APP,
then export_report for either format. Rendering never reloads current cost data.
"""
from __future__ import annotations

from report.model import ReportError, build_report_payload, verify_payload
from report.export import export_report
from report.narrative_adapter import build_report_attribution_narrative


def build_report_bundle(params, tables=None, *, evidence=None, model_fn=None,
                        model_version="not_configured", versions=None, evidence_fn=None):
    """Return one sealed payload and DOCX/PDF exports of that same snapshot."""
    payload = build_report_payload(params, tables, evidence=evidence, model_fn=model_fn,
                                   model_version=model_version, versions=versions, evidence_fn=evidence_fn)
    return {"payload": payload, "docx": export_report(payload, "docx"),
            "pdf": export_report(payload, "pdf")}


__all__ = ["ReportError", "build_report_payload", "build_report_bundle", "export_report", "verify_payload",
           "build_report_attribution_narrative"]

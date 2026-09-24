"""Bind task suggestions to the exact existing analysis evidence records.

These are canonical record digests, not claimed original-document byte hashes.
The document byte hash, when present, is itself part of the frozen record.
"""
from collections import Counter
from hashlib import sha256
from enterprise.task_workflow import canonical


def frozen_evidence_hashes(evidence, evidence_ids):
    rows=[row for row in evidence or [] if isinstance(row,dict) and isinstance(row.get('id'),str)]
    counts=Counter(row['id'] for row in rows)
    selected=set(evidence_ids or [])
    return {row['id']:sha256(canonical(row).encode('utf-8')).hexdigest()
            for row in rows if row['id'] in selected and counts[row['id']]==1
            and row.get('source') and (row.get('text') or row.get('content'))}

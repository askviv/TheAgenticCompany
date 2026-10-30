"""Run a read-only native company evidence snapshot twice and compare versions.

This script runs inside the Frappe admin container. Its private input JSON is
``{"company": ..., "entity_refs": [{"doctype": ..., "name": ...}]}``.
It prints only counts and a content hash, never document names or fields.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


SITE = "erp-sim-4472c9ce758f548598f1be6379f9f42b.localhost"
BENCH_ROOT = Path("/home/frappe/frappe-bench")
SITES_ROOT = BENCH_ROOT / "sites"
ADMIN_ROOT = Path("/opt/erpnext-admin")
sys.path.insert(0, str(BENCH_ROOT / "apps"))
sys.path.insert(0, str(ADMIN_ROOT))

import frappe

from evaluation_evidence import (
    capture_company_evaluation_evidence,
    evaluation_evidence_version_manifest,
)


def capture_once(company: str, entity_refs: list[dict[str, str]]) -> tuple[int, int, str]:
    os.chdir(SITES_ROOT)
    frappe.init(SITE, sites_path=str(SITES_ROOT))
    frappe.connect()
    frappe.set_user("Administrator")
    failure = None
    try:
        snapshot = capture_company_evaluation_evidence(entity_refs, company)
        version = evaluation_evidence_version_manifest(snapshot)
        return snapshot["scope"]["document_count"], len(snapshot["documents"]), version["sha256"]
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            try:
                frappe.db.rollback()
            except Exception as rollback_error:
                if failure is None:
                    raise
                failure.add_note(f"Read-only evidence rollback failed: {type(rollback_error).__name__}")
        finally:
            frappe.destroy()
            os.chdir(BENCH_ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    company = payload.get("company")
    entity_refs = payload.get("entity_refs")
    if not isinstance(company, str) or not isinstance(entity_refs, list):
        raise ValueError("private evidence probe input requires company and entity_refs")
    first_count, first_groups, first_hash = capture_once(company, entity_refs)
    second_count, second_groups, second_hash = capture_once(company, entity_refs)
    same_versions = first_hash == second_hash
    print(json.dumps({
        "snapshot_document_count": second_count,
        "snapshot_doctype_count": second_groups,
        "repeat_capture_matches": same_versions,
        "version_manifest_sha256": second_hash,
    }, sort_keys=True))
    if (first_count != second_count or first_groups != second_groups or not same_versions):
        raise RuntimeError("native evaluation evidence changed between read-only captures")


if __name__ == "__main__":
    main()

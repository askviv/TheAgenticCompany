"""Durable company evidence and benchmark projection ledger, on existing SQLite."""

from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import fcntl
import os
import json
from typing import Any
from .feedback_contracts import (
    CompanyEvidence,
    RecordMapping,
    TranslationResult,
    NativeActionReceipt,
)
from .store import SQLiteStore, StoreConflict


def encoded(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def identity(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


class CompanyFeedback:
    def __init__(self, store: SQLiteStore, adapters: dict):
        self.store, self.adapters = store, adapters
        with store.transaction() as db:
            schema = """
                CREATE TABLE IF NOT EXISTS company_feedback_runtime_reports(
                    provider TEXT PRIMARY KEY,result_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS company_record_mappings(
                    mapping_id TEXT PRIMARY KEY,benchmark TEXT NOT NULL,doctype TEXT NOT NULL,
                    name TEXT NOT NULL,experiment_id TEXT,mapping_json TEXT NOT NULL,baseline_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS company_evidence_threads(
                    mapping_id TEXT NOT NULL,message_id TEXT NOT NULL,PRIMARY KEY(mapping_id,message_id));
                CREATE TABLE IF NOT EXISTS company_evidence(
                    evidence_id TEXT PRIMARY KEY,doctype TEXT NOT NULL,name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,received_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS company_feedback_actions(
                    action_id TEXT PRIMARY KEY,mapping_id TEXT NOT NULL,evidence_id TEXT NOT NULL,
                    stage TEXT NOT NULL,translation_json TEXT NOT NULL,receipt_json TEXT,last_error TEXT);
            """
            # Retain the existing BEGIN IMMEDIATE lock during schema migration.
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(company_evidence)")
            }
            if "translation_status" not in columns:
                db.execute(
                    "ALTER TABLE company_evidence ADD COLUMN translation_status TEXT NOT NULL DEFAULT 'queued'"
                )

    def map_record(self, mapping: RecordMapping, starting_snapshot: dict) -> None:
        if (starting_snapshot.get("doctype"), starting_snapshot.get("name")) != (
            mapping.erp_doctype,
            mapping.erp_name,
        ):
            raise ValueError(
                "Starting snapshot does not identify the mapped ERP record"
            )
        payload, baseline = mapping.model_dump_json(), encoded(starting_snapshot)
        with self.store.transaction() as db:
            prior = db.execute(
                "SELECT * FROM company_record_mappings WHERE mapping_id=?",
                (mapping.mapping_id,),
            ).fetchone()
            if prior:
                if (
                    prior["mapping_json"] != payload
                    or prior["baseline_json"] != baseline
                ):
                    raise StoreConflict("Record mapping or starting snapshot changed")
                return
            db.execute(
                "INSERT INTO company_record_mappings VALUES(?,?,?,?,?,?,?)",
                (
                    mapping.mapping_id,
                    mapping.benchmark,
                    mapping.erp_doctype,
                    mapping.erp_name,
                    mapping.experiment_id,
                    payload,
                    baseline,
                ),
            )
        # Native webhooks can arrive before the source import is registered.
        # Replay the same immutable evidence through newly known mappings.
        with self.store.transaction() as db:
            pending = [
                CompanyEvidence.model_validate_json(r["payload_json"])
                for r in db.execute(
                    "SELECT payload_json FROM company_evidence ORDER BY received_at,evidence_id"
                )
            ]
        for evidence in pending:
            if (mapping.erp_doctype, mapping.erp_name) in self._references(
                evidence
            ) or evidence.in_reply_to in mapping.message_ids:
                self.observe(evidence)

    def link_message(self, mapping_id: str, message_id: str) -> None:
        with self.store.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM company_record_mappings WHERE mapping_id=?",
                (mapping_id,),
            ).fetchone():
                raise KeyError("Record mapping does not exist")
            db.execute(
                "INSERT OR IGNORE INTO company_evidence_threads VALUES(?,?)",
                (mapping_id, message_id),
            )

    def mappings(self) -> list[RecordMapping]:
        with self.store.transaction() as db:
            result = [
                RecordMapping.model_validate_json(r["mapping_json"])
                for r in db.execute(
                    "SELECT mapping_json FROM company_record_mappings ORDER BY mapping_id"
                )
            ]
            for mapping in result:
                mapping.message_ids = sorted(
                    set(mapping.message_ids)
                    | {
                        r["message_id"]
                        for r in db.execute(
                            "SELECT message_id FROM company_evidence_threads WHERE mapping_id=?",
                            (mapping.mapping_id,),
                        )
                    }
                )
            return result

    @staticmethod
    def _references(evidence: CompanyEvidence) -> set[tuple[str, str]]:
        refs = {(evidence.erp_doctype, evidence.erp_name)}
        for field in ("amended_from", "return_against"):
            if evidence.document.get(field):
                refs.add((evidence.erp_doctype, str(evidence.document[field])))
        linked = {
            "purchase_order": "Purchase Order",
            "sales_order": "Sales Order",
            "against_sales_order": "Sales Order",
            "purchase_invoice": "Purchase Invoice",
            "sales_invoice": "Sales Invoice",
            "purchase_receipt": "Purchase Receipt",
            "delivery_note": "Delivery Note",
            "custom_customer_service_order": "Sales Order",
            "item_code": "Item",
        }
        children = []
        if evidence.event not in ("email_delivered", "email_reply"):
            for field in ("items", "references", "accounts"):
                children.extend(evidence.document.get(field) or [])
        for row in [evidence.document, *children]:
            for field, doctype in linked.items():
                if row.get(field):
                    refs.add((doctype, str(row[field])))
            if row.get("reference_doctype") and row.get("reference_name"):
                refs.add((str(row["reference_doctype"]), str(row["reference_name"])))
        return refs

    def with_observed_history(self, evidence: CompanyEvidence) -> CompanyEvidence:
        if evidence.before_document is not None:
            return evidence
        candidates = []
        with self.store.transaction() as db:
            prior = db.execute(
                "SELECT payload_json FROM company_evidence WHERE evidence_id=?",
                (evidence.evidence_id,),
            ).fetchone()
            if prior:
                # Retries retain the same originally observed predecessor.
                return CompanyEvidence.model_validate_json(prior["payload_json"])
            for row in db.execute(
                "SELECT payload_json FROM company_evidence WHERE doctype=? AND name=?",
                (evidence.erp_doctype, evidence.erp_name),
            ):
                earlier = CompanyEvidence.model_validate_json(row["payload_json"])
                if earlier.occurred_at < evidence.occurred_at:
                    candidates.append((earlier.occurred_at, earlier.document))
            for row in db.execute(
                "SELECT baseline_json FROM company_record_mappings WHERE doctype=? AND name=?",
                (evidence.erp_doctype, evidence.erp_name),
            ):
                doc = json.loads(row["baseline_json"])
                if (
                    doc.get("modified")
                    and evidence.document.get("modified")
                    and doc["modified"] < evidence.document["modified"]
                ):
                    # Baseline uses native ERP timestamp ordering; no invented prior state.
                    candidates.append((datetime.min.replace(tzinfo=timezone.utc), doc))
        if candidates:
            return evidence.model_copy(
                update={"before_document": max(candidates, key=lambda pair: pair[0])[1]}
            )
        return evidence

    def capture(self, evidence: CompanyEvidence) -> CompanyEvidence:
        if evidence.event not in ("email_delivered", "email_reply") and (
            evidence.document.get("doctype"),
            evidence.document.get("name"),
        ) != (evidence.erp_doctype, evidence.erp_name):
            raise ValueError("Evidence document identity mismatch")
        raw_evidence = evidence
        evidence = self.with_observed_history(evidence)
        if evidence.model_dump(
            mode="json", exclude={"before_document", "company_time"}
        ) != raw_evidence.model_dump(
            mode="json", exclude={"before_document", "company_time"}
        ):
            raise StoreConflict("Evidence identity reused with different facts")
        data = encoded(evidence.model_dump(mode="json"))
        with self.store.transaction() as db:
            prior = db.execute(
                "SELECT payload_json FROM company_evidence WHERE evidence_id=?",
                (evidence.evidence_id,),
            ).fetchone()
            if prior and CompanyEvidence.model_validate_json(
                prior["payload_json"]
            ).model_dump(mode="json") != evidence.model_dump(mode="json"):
                raise StoreConflict("Evidence identity reused with different facts")
            if not prior:
                db.execute(
                    "INSERT INTO company_evidence(evidence_id,doctype,name,payload_json,received_at) VALUES(?,?,?,?,?)",
                    (
                        evidence.evidence_id,
                        evidence.erp_doctype,
                        evidence.erp_name,
                        data,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        return evidence

    def observe(
        self, evidence: CompanyEvidence, *, refresh: bool = False
    ) -> list[dict]:
        evidence = self.capture(evidence)
        references = self._references(evidence)
        if evidence.erp_doctype == "Payment Entry":
            # A submitted payment normally links to a Sales Invoice, while a
            # company feedback mapping belongs to the source Sales Order. Join
            # only persisted invoice evidence, never a live ERP lookup.
            invoice_names = {
                name for doctype, name in references if doctype == "Sales Invoice"
            }
            with self.store.transaction() as db:
                invoice_rows = [
                    row["payload_json"]
                    for invoice_name in invoice_names
                    for row in db.execute(
                        "SELECT payload_json FROM company_evidence "
                        "WHERE doctype='Sales Invoice' AND name=?",
                        (invoice_name,),
                    )
                ]
            by_invoice = {}
            for payload in invoice_rows:
                invoice = CompanyEvidence.model_validate_json(payload)
                key = invoice.erp_name
                ordering = (
                    str(invoice.document.get("modified", "")),
                    invoice.occurred_at.isoformat(),
                    invoice.evidence_id,
                )
                if key not in by_invoice or ordering > by_invoice[key][0]:
                    by_invoice[key] = (ordering, invoice)
            for _, invoice in by_invoice.values():
                if invoice.event in ("on_submit", "on_update_after_submit") and invoice.document.get("docstatus") == 1:
                    references.update(self._references(invoice))
        mappings = [
            m
            for m in self.mappings()
            if (m.erp_doctype, m.erp_name) in references
            or (
                {evidence.in_reply_to, evidence.message_id}
                | set(str(evidence.document.get("references") or "").split())
            )
            & set(m.message_ids)
        ]
        # An email without a matching thread must never enter another task's evidence.
        experiment_ids = {m.experiment_id for m in mappings if m.experiment_id}
        ambiguous = len(experiment_ids) > 1 or bool(
            evidence.experiment_id
            and experiment_ids
            and evidence.experiment_id not in experiment_ids
        )
        results = []
        for mapping in mappings:
            key = hashlib.sha256(
                f"{mapping.mapping_id}:{evidence.evidence_id}".encode()
            ).hexdigest()
            with self.store.transaction() as db:
                existing = db.execute(
                    "SELECT * FROM company_feedback_actions WHERE action_id=?", (key,)
                ).fetchone()
            if existing and not (
                refresh
                and existing["stage"] == "inconclusive"
                and existing["receipt_json"] is None
            ):
                results.append(self._result(existing))
                continue
            if ambiguous:
                result = TranslationResult(
                    status="inconclusive",
                    reason="Shared ERP record has overlapping experiment attribution",
                    coverage=["shared_company_interaction"],
                )
            elif mapping.benchmark not in self.adapters:
                result = TranslationResult(
                    status="unmapped",
                    reason="Native feedback adapter is not configured",
                )
            else:
                result = TranslationResult.model_validate(
                    self.adapters[mapping.benchmark].translate(mapping, evidence)
                )
            if result.action:
                action = result.action
                if (action.mapping_id, action.evidence_id, action.benchmark) != (
                    mapping.mapping_id,
                    evidence.evidence_id,
                    mapping.benchmark,
                ):
                    raise ValueError(
                        "Translator returned an action for different evidence or mapping"
                    )
                action.action_id = key
                action.company_time = action.company_time or evidence.company_time
            if result.status == "supported" and result.action is None:
                raise ValueError(
                    "Supported translation requires an actual native action"
                )
            with self.store.transaction() as db:
                db.execute(
                    "INSERT OR IGNORE INTO company_feedback_actions VALUES(?,?,?,?,?,?,?)",
                    (
                        key,
                        mapping.mapping_id,
                        evidence.evidence_id,
                        "pending" if result.status == "supported" else result.status,
                        result.model_dump_json(),
                        None,
                        None,
                    ),
                )
                if refresh:
                    db.execute(
                        "UPDATE company_feedback_actions SET stage=?,translation_json=? WHERE action_id=? AND stage='inconclusive' AND receipt_json IS NULL",
                        (
                            "pending"
                            if result.status == "supported"
                            else result.status,
                            result.model_dump_json(),
                            key,
                        ),
                    )
                row = db.execute(
                    "SELECT * FROM company_feedback_actions WHERE action_id=?", (key,)
                ).fetchone()
            results.append(self._result(row))
        with self.store.transaction() as db:
            db.execute(
                "UPDATE company_evidence SET translation_status=? WHERE evidence_id=?",
                ("done" if mappings else "unmapped", evidence.evidence_id),
            )
        return results or [
            {
                "status": "unmapped",
                "reason": "No mapped company record or email thread",
                "evidence_id": evidence.evidence_id,
            }
        ]

    def translate_captured(self) -> None:
        with self.store.transaction() as db:
            rows = [
                CompanyEvidence.model_validate_json(row["payload_json"])
                for row in db.execute(
                    "SELECT payload_json FROM company_evidence WHERE translation_status='queued' OR evidence_id IN "
                    "(SELECT evidence_id FROM company_feedback_actions WHERE stage='inconclusive' AND receipt_json IS NULL) "
                    "ORDER BY CASE WHEN translation_status='queued' THEN 0 ELSE 1 END,received_at,evidence_id LIMIT 100"
                )
            ]
        failures = []
        for evidence in rows:
            try:
                self.observe(evidence, refresh=True)
            except Exception as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("Company evidence translation failed", failures)

    @staticmethod
    def _result(row) -> dict:
        return {
            "action_id": row["action_id"],
            "status": row["stage"],
            "translation": json.loads(row["translation_json"]),
            "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
            "error_type": row["last_error"],
        }

    def reconcile(self) -> None:
        with self.store.transaction() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM company_feedback_actions WHERE stage IN ('pending','submitting','uncertain') ORDER BY rowid"
                )
            ]
        rows.sort(
            key=lambda row: (
                TranslationResult.model_validate_json(
                    row["translation_json"]
                ).action.company_time
                or datetime.min.replace(tzinfo=timezone.utc),
                row["evidence_id"],
            )
        )
        errors = []
        for row in rows:
            path = self.store.db_path.parent / (
                "feedback-action-" + row["action_id"] + ".lock"
            )
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # Another worker owns this exact action.
                with self.store.transaction() as db:
                    current = db.execute(
                        "SELECT * FROM company_feedback_actions WHERE action_id=?",
                        (row["action_id"],),
                    ).fetchone()
                if current["stage"] not in ("pending", "submitting", "uncertain"):
                    continue
                try:
                    self._reconcile_one(dict(current))
                except Exception as error:
                    errors.append(error)
            finally:
                os.close(descriptor)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Native company feedback actions failed", errors)

    def _reconcile_one(self, row) -> None:
        action = TranslationResult.model_validate_json(row["translation_json"]).action
        adapter = self.adapters[action.benchmark]
        try:
            # Each native adapter must atomically persist its checkpoint and action receipt.
            # An absent native receipt permits retrying that same idempotent action.
            receipt = adapter.reconcile(action)
            if receipt is None:
                with self.store.transaction() as db:
                    claimed = db.execute(
                        "UPDATE company_feedback_actions SET stage='submitting' WHERE action_id=? AND stage IN ('pending','submitting','uncertain')",
                        (action.action_id,),
                    ).rowcount
                if not claimed:
                    return
                receipt = adapter.apply(action)
            result = NativeActionReceipt.model_validate(receipt)
            if result.action_id != action.action_id:
                raise ValueError(
                    "Native benchmark receipt identifies a different action"
                )
            if result.outcome.get("status") == "queued":
                stage = "pending"
            else:
                stage = (
                    "reconciled"
                    if result.accepted and result.reconciled
                    else "inconclusive"
                )
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE company_feedback_actions SET stage=?,receipt_json=?,last_error=NULL WHERE action_id=?",
                    (stage, result.model_dump_json(), action.action_id),
                )
        except Exception as error:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE company_feedback_actions SET stage='uncertain',last_error=? WHERE action_id=?",
                    (type(error).__name__, action.action_id),
                )
            raise

    def activity(self) -> list[dict]:
        labels = {
            "on_submit": "submitted",
            "on_cancel": "cancelled",
            "on_update": "updated",
            "on_update_after_submit": "updated after submission",
            "on_trash": "deleted",
        }
        priority = {
            "on_update": 1,
            "on_submit": 2,
            "on_update_after_submit": 2,
            "on_cancel": 3,
            "on_trash": 4,
        }
        with self.store.transaction() as db:
            observations = [
                CompanyEvidence.model_validate_json(row["payload_json"])
                for row in db.execute("SELECT payload_json FROM company_evidence")
            ]
        changes = {}
        for evidence in observations:
            if evidence.event not in labels:
                continue
            key = (
                evidence.erp_doctype,
                evidence.erp_name,
                evidence.document.get("modified"),
            )
            prior = changes.get(key)
            if prior is None or priority[evidence.event] > priority[prior.event]:
                changes[key] = evidence
        return [
            {
                "id": "company-evidence:" + e.evidence_id,
                "timestamp": e.occurred_at.isoformat(),
                "event": "COMPANY_DOCUMENT_CHANGED",
                "type": "action",
                "actor": e.actor,
                "title": f"{e.erp_doctype} {labels[e.event]}",
                "description": f"{e.erp_doctype} {e.erp_name} was {labels[e.event]}.",
                "summary": f"{e.erp_doctype} {e.erp_name} was {labels[e.event]}.",
            }
            for e in changes.values()
        ]

    def report(
        self, experiment_id: str | None = None, *, evaluate_native: bool = True
    ) -> dict:
        mappings = [
            m
            for m in self.mappings()
            if experiment_id is None or m.experiment_id == experiment_id
        ]
        ids = {m.mapping_id for m in mappings}
        with self.store.transaction() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM company_feedback_actions ORDER BY rowid"
                )
                if r["mapping_id"] in ids
            ]
            evidence_ids = {r["evidence_id"] for r in rows}
            evidence = [
                CompanyEvidence.model_validate_json(r["payload_json"])
                for r in db.execute("SELECT * FROM company_evidence")
                if r["evidence_id"] in evidence_ids
            ]
        outcomes = [
            {
                "event": e.event,
                "doctype": e.erp_doctype,
                "name": e.erp_name,
                "actor": e.actor,
                "occurred_at": e.occurred_at.isoformat(),
                "docstatus": e.document.get("docstatus"),
                "evidence_refs": e.evidence_refs,
                "actor_provenance": (
                    "ERPNext modified_by"
                    if e.event not in ("email_delivered", "email_reply")
                    else "authenticated sender receipt"
                    if e.authenticated_sender_actor_id
                    else "email sender claim"
                ),
            }
            for e in evidence
        ]
        shared = {}
        for mapping in self.mappings():
            if mapping.experiment_id:
                shared.setdefault((mapping.erp_doctype, mapping.erp_name), set()).add(
                    mapping.experiment_id
                )
        changed_refs = (
            set().union(
                *(
                    self._references(e)
                    for e in evidence
                    if e.event not in ("email_delivered", "email_reply")
                )
            )
            if evidence
            else set()
        )
        interactions = list(
            {
                (m.erp_doctype, m.erp_name): {
                    "doctype": m.erp_doctype,
                    "name": m.erp_name,
                    "experiments": sorted(shared[(m.erp_doctype, m.erp_name)]),
                }
                for m in mappings
                if (m.erp_doctype, m.erp_name) in changed_refs
                and len(shared.get((m.erp_doctype, m.erp_name), set())) > 1
            }.values()
        )
        native = []
        if evaluate_native and not interactions:
            for benchmark in sorted({m.benchmark for m in mappings}):
                if benchmark in self.adapters:
                    native.append(
                        {
                            "benchmark": benchmark,
                            "result": self.adapters[benchmark].evaluate(
                                [m for m in mappings if m.benchmark == benchmark],
                                evidence,
                            ),
                        }
                    )
        return {
            "evaluation_label": "company-adapted evaluation",
            "shared_company_interactions": interactions,
            "integration": [self._result(r) for r in rows],
            "business_outcome": outcomes,
            "native_benchmark_result": native,
            "coverage": {
                "mapped_records": len(mappings),
                "unmapped": sum(r["stage"] == "unmapped" for r in rows),
                "inconclusive": sum(
                    r["stage"] in ("inconclusive", "uncertain") for r in rows
                ),
            },
            "attribution": "inconclusive"
            if interactions
            or any(
                "shared_company_interaction"
                in json.loads(r["translation_json"]).get("coverage", [])
                for r in rows
            )
            else "mapped_records_only",
        }

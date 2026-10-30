from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from servers.simulation.company_feedback import CompanyFeedback
from servers.simulation.feedback_contracts import (
    CompanyEvidence,
    RecordMapping,
    TranslationResult,
    TranslatedAction,
    NativeActionReceipt,
)
from servers.simulation.store import SQLiteStore, StoreConflict

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def mapping(experiment="exp1", key="map1"):
    return RecordMapping(
        mapping_id=key,
        benchmark="ecommercebench",
        upstream_commit="a" * 40,
        source_record_id="shipment1",
        source_entity_type="shipment",
        erp_doctype="Sales Order",
        erp_name="SO1",
        experiment_id=experiment,
        company_time=NOW,
    )


def evidence(key="event1", status=1):
    return CompanyEvidence(
        evidence_id=key,
        erp_doctype="Delivery Note",
        erp_name="DN1",
        event="on_submit",
        actor="employee",
        occurred_at=NOW,
        company_time=NOW,
        document={
            "doctype": "Delivery Note",
            "name": "DN1",
            "docstatus": status,
            "items": [{"against_sales_order": "SO1"}],
        },
        evidence_refs=["https://erp.example/app/delivery-note/DN1"],
    )


class Adapter:
    def __init__(self):
        self.receipts = {}
        self.calls = 0

    def translate(self, m, e):
        if e.document["docstatus"] != 1:
            return TranslationResult(
                status="unmapped", reason="Draft is not fulfilment"
            )
        return TranslationResult(
            status="supported",
            reason="Exact native shipment",
            action=TranslatedAction(
                action_id="pending",
                mapping_id=m.mapping_id,
                evidence_id=e.evidence_id,
                benchmark=m.benchmark,
                native_action="ship_orders",
                arguments={"shipment_ids": ["shipment1"]},
            ),
        )

    def reconcile(self, a):
        return self.receipts.get(a.action_id)

    def apply(self, a):
        self.calls += 1
        self.receipts[a.action_id] = NativeActionReceipt(
            action_id=a.action_id,
            accepted=True,
            reconciled=True,
            native_state_ref="checkpoint1",
        )
        return self.receipts[a.action_id]

    def evaluate(self, m, e):
        return {"native_receipts": len(self.receipts), "score_claimed": False}


def setup(tmp_path, adapter):
    flow = CompanyFeedback(
        SQLiteStore(tmp_path / "state.sqlite"), {"ecommercebench": adapter}
    )
    flow.map_record(
        mapping(), {"doctype": "Sales Order", "name": "SO1", "docstatus": 0}
    )
    return flow


def test_submitted_child_record_updates_native_once_after_restart(tmp_path):
    a = Adapter()
    f = setup(tmp_path, a)
    f.observe(evidence())
    f.reconcile()
    restart = setup(tmp_path, a)
    restart.observe(evidence())
    restart.reconcile()
    assert a.calls == 1
    activity = restart.activity()
    assert len(activity) == 1
    assert activity[0]["title"] == "Delivery Note submitted"
    assert "DN1" in activity[0]["description"]
    report = restart.report("exp1")
    assert report["integration"][0]["status"] == "reconciled"
    assert report["business_outcome"][0]["docstatus"] == 1
    assert report["native_benchmark_result"][0]["result"]["native_receipts"] == 1


def test_draft_and_unrelated_records_never_execute_native_action(tmp_path):
    a = Adapter()
    f = setup(tmp_path, a)
    assert f.observe(evidence(status=0))[0]["status"] == "unmapped"
    other = evidence("event2")
    other.document["items"] = []
    assert f.observe(other)[0]["status"] == "unmapped"
    f.reconcile()
    assert a.calls == 0


def test_duplicate_identity_with_changed_evidence_or_baseline_is_conflict(tmp_path):
    f = setup(tmp_path, Adapter())
    f.observe(evidence())
    with pytest.raises(StoreConflict):
        f.observe(evidence(status=0))
    with pytest.raises(StoreConflict):
        f.map_record(
            mapping(), {"doctype": "Sales Order", "name": "SO1", "docstatus": 1}
        )


def test_shared_record_ambiguity_is_inconclusive_and_never_applied(tmp_path):
    a = Adapter()
    f = setup(tmp_path, a)
    f.map_record(
        mapping("exp2", "map2"),
        {"doctype": "Sales Order", "name": "SO1", "docstatus": 0},
    )
    assert {r["status"] for r in f.observe(evidence())} == {"inconclusive"}
    f.reconcile()
    assert a.calls == 0
    assert f.report("exp1")["attribution"] == "inconclusive"
    assert f.report("exp1")["native_benchmark_result"] == []


def test_crash_after_native_commit_recovers_receipt_without_repeating_action(tmp_path):
    a = Adapter()
    f = setup(tmp_path, a)
    f.observe(evidence())
    original = a.apply

    def lose_receipt(action):
        original(action)
        raise ConnectionError("interrupted after native checkpoint commit")

    a.apply = lose_receipt
    with pytest.raises(ConnectionError):
        f.reconcile()
    setup(tmp_path, a).reconcile()
    assert a.calls == 1
    assert f.report()["integration"][0]["status"] == "reconciled"


def test_pre_submit_crash_retries_same_native_identity(tmp_path):
    a = Adapter()
    f = setup(tmp_path, a)
    f.observe(evidence())
    with f.store.transaction() as db:
        db.execute("UPDATE company_feedback_actions SET stage='submitting'")
    setup(tmp_path, a).reconcile()
    assert a.calls == 1
    assert f.report()["integration"][0]["status"] == "reconciled"


def test_native_webhook_requires_correct_signature_before_durable_observation(tmp_path):
    import base64, hashlib, hmac, json
    from fastapi.testclient import TestClient
    from servers.simulation.app import create_app
    from servers.simulation.evidence_observer import native_document_evidence

    a = Adapter()
    flow = setup(tmp_path, a)
    runtime = SimpleNamespace(secret="fixture-secret")
    runtime.accept = lambda event: flow.observe(
        native_document_evidence(
            event, company_time=NOW, erp_public_url="https://erp.example"
        )
    )
    app = create_app(
        SimpleNamespace(company_feedback=runtime),
        {"controller-fixture": {"actor_id": "controller", "role": "controller"}},
    )
    payload = {
        "event": "on_submit",
        "erp_timezone": "UTC",
        "document": {
            "doctype": "Delivery Note",
            "name": "DN1",
            "company": "The Agent Company",
            "modified": "2026-10-30 00:00:00",
            "modified_by": "employee",
            "docstatus": 1,
            "items": [{"against_sales_order": "SO1"}],
        },
    }
    body = json.dumps(payload).encode()
    signature = base64.b64encode(
        hmac.new(runtime.secret.encode(), body, hashlib.sha256).digest()
    ).decode()
    with TestClient(app) as client:
        assert client.post("/company/evidence/native", content=body).status_code == 401
        response = client.post(
            "/company/evidence/native",
            content=body,
            headers={"X-Frappe-Webhook-Signature": signature},
        )
        assert response.status_code == 200
        assert response.json()["evidence"][0]["status"] == "pending"
        assert (
            client.post(
                "/company/evidence/native",
                content=body + b" ",
                headers={"X-Frappe-Webhook-Signature": signature},
            ).status_code
            == 401
        )
    flow.reconcile()
    assert a.calls == 1


def test_concurrent_reconcilers_hold_one_native_action_claim(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    a = Adapter()
    first = setup(tmp_path, a)
    second = setup(tmp_path, a)
    first.observe(evidence())
    entered = Event()
    release = Event()
    apply = a.apply

    def block(action):
        entered.set()
        assert release.wait(timeout=5)
        return apply(action)

    a.apply = block
    with ThreadPoolExecutor(max_workers=2) as executor:
        running = executor.submit(first.reconcile)
        assert entered.wait(timeout=5)
        second.reconcile()
        release.set()
        running.result(timeout=5)
    assert a.calls == 1
    assert first.report()["integration"][0]["status"] == "reconciled"


def test_native_retry_preserves_first_company_time_and_email_thread_is_scoped(tmp_path):
    a = Adapter()
    f = setup(tmp_path, a)
    first = evidence()
    f.observe(first)
    repeated = first.model_copy(
        update={"company_time": datetime(2026, 10, 31, tzinfo=timezone.utc)}
    )
    f.observe(repeated)
    with f.store.transaction() as db:
        row = db.execute("SELECT payload_json FROM company_evidence").fetchone()
    assert CompanyEvidence.model_validate_json(row["payload_json"]).company_time == NOW
    f.link_message("map1", "<root@example.test>")
    reply = CompanyEvidence(
        evidence_id="reply1",
        erp_doctype="Communication",
        erp_name="<reply@example.test>",
        event="email_reply",
        actor="claimed-sender",
        occurred_at=NOW,
        company_time=NOW,
        document={"references": "<root@example.test>"},
        evidence_refs=["mailbox://receipt"],
        message_id="<reply@example.test>",
    )
    # A communication is routed to the mapped thread; it never gets mistaken
    # for a submitted financial record by the native translator.
    a.translate = lambda m, e: TranslationResult(
        status="unmapped", reason="Email is not a posted payment"
    )
    assert (
        f.observe(reply)[0]["translation"]["reason"] == "Email is not a posted payment"
    )


def test_capture_acknowledges_before_translation_and_later_evidence_unblocks_action(
    tmp_path,
):
    adapter = Adapter()
    flow = setup(tmp_path, adapter)
    native_translate = adapter.translate
    proof = {"posted": False}

    def translate(mapping, evidence):
        if not proof["posted"]:
            return TranslationResult(
                status="inconclusive", reason="Posted refund is not yet observed"
            )
        result = native_translate(mapping, evidence)
        result.action.supporting_evidence = [
            {"doctype": "Payment Entry", "name": "REFUND1", "docstatus": 1}
        ]
        return result

    adapter.translate = translate
    flow.capture(evidence())
    with flow.store.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM company_feedback_actions").fetchone()[0]
            == 0
        )
    flow.translate_captured()
    flow.reconcile()
    assert adapter.calls == 0
    assert (
        flow.report(evaluate_native=False)["integration"][0]["status"] == "inconclusive"
    )
    proof["posted"] = True
    flow.translate_captured()
    flow.reconcile()
    flow.translate_captured()
    flow.reconcile()
    assert adapter.calls == 1
    result = flow.report(evaluate_native=False)["integration"][0]
    assert result["status"] == "reconciled"
    assert (
        result["translation"]["action"]["supporting_evidence"][0]["name"] == "REFUND1"
    )


def test_submitted_purchase_order_reaches_exact_source_item_mapping(tmp_path):
    adapter = Adapter()
    flow = CompanyFeedback(SQLiteStore(tmp_path / 'state.sqlite'), {'ecommercebench': adapter})
    item = mapping().model_copy(update={'erp_doctype': 'Item', 'erp_name': 'SOURCE-SKU'})
    flow.map_record(item, {'doctype': 'Item', 'name': 'SOURCE-SKU'})
    purchase = evidence().model_copy(update={
        'erp_doctype': 'Purchase Order', 'erp_name': 'PO-1',
        'document': {'doctype': 'Purchase Order', 'name': 'PO-1', 'docstatus': 1,
                     'items': [{'item_code': 'SOURCE-SKU', 'qty': 2}]}})
    flow.observe(purchase)
    flow.reconcile()
    assert adapter.calls == 1
    assert flow.report('exp1')['native_benchmark_result'][0]['result']['native_receipts'] == 1
    unrelated = purchase.model_copy(update={'evidence_id': 'unrelated', 'erp_name': 'PO-2',
        'document': {'doctype': 'Purchase Order', 'name': 'PO-2', 'docstatus': 1,
                     'items': [{'item_code': 'UNRELATED-SKU', 'qty': 2}]}})
    flow.observe(unrelated)
    flow.reconcile()
    assert adapter.calls == 1

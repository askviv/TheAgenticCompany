from __future__ import annotations

import json
import hashlib
import sqlite3
import stat
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import requests
from unittest.mock import patch

from servers.simulation.erpnext import (
    ERPNextAdapter, ERPNextConflict, ERPNextError, ERPNextSiteAdmin, VerifiedERPApproval,
    VerifiedShippingApproval,
)


class Response:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError("provider response body must not escape")
            error.response = self
            raise error


class ScriptedHTTP:
    """A request transport stub; adapter parsing and workflow behavior remain real."""

    def __init__(self, handler):
        self.handler = handler
        self.headers = {}
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)


class ERPNextAdapterTests(unittest.TestCase):
    def make_adapter(self, http, directory, **kwargs):
        return ERPNextAdapter(
            "http://erp.test",
            "key",
            "secret",
            Path(directory) / "idempotency.sqlite",
            "erp-sim-0123456789abcdef0123456789abcdef.localhost",
            session=http,
            **kwargs,
        )

    def test_a1_uses_submitted_receipt_rows_linked_to_the_same_po_item(self):
        def response(method, url, kwargs):
            if url.endswith("/Purchase%20Invoice/PINV-1"):
                return Response({"data": {
                    "name": "PINV-1", "docstatus": 0, "workflow_state": "Draft",
                    "supplier": "SUP-1", "company": "CO-1",
                    "items": [{"item_code": "COMP-1", "qty": 10000,
                               "purchase_order": "PO-1", "po_detail": "POI-1"}],
                }})
            if url.endswith("/Purchase%20Receipt"):
                return Response({"data": [
                    {"name": "PR-1"}, {"name": "PR-DRAFT"},
                ]})
            if url.endswith("/Purchase%20Receipt/PR-1"):
                return Response({"data": {"name": "PR-1", "docstatus": 1,
                    "posting_date": "2026-10-06", "posting_time": "9:00:00", "items": [
                    {"purchase_order": "PO-1", "purchase_order_item": "POI-1",
                     "received_qty": 8000, "qty": 8000, "item_code": "COMP-1"},
                ]}})
            if url.endswith("/Purchase%20Receipt/PR-DRAFT"):
                return Response({"data": {"name": "PR-DRAFT", "docstatus": 0,
                    "posting_date": "2026-10-06", "posting_time": "9:00:00", "items": [
                    {"purchase_order": "PO-1", "purchase_order_item": "POI-1",
                     "received_qty": 2000, "qty": 2000, "item_code": "COMP-1"},
                ]}})
            self.fail(f"Unexpected provider request: {method} {url}")

        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make_adapter(ScriptedHTTP(response), directory)
            assessment = adapter.assess_invoice("PINV-1")

        self.assertEqual(assessment["lines"][0]["billed_qty"], Decimal("10000"))
        self.assertEqual(assessment["lines"][0]["received_qty"], Decimal("8000"))
        self.assertEqual(assessment["lines"][0]["unreceived_billed_qty"], Decimal("2000"))
        self.assertTrue(assessment["lines"][0]["overbilled"])
        self.assertEqual(assessment["lines"][0]["receipt_ids"], ["PR-1"])

    def test_shipping_commit_creates_separate_native_service_po_and_recovers_idempotently(self):
        run_id = "01234567-89ab-cdef-0123-456789abcdef"
        site = "erp-sim-0123456789abcdef0123456789abcdef.localhost"
        original = {"name": "PO-COMPONENT", "supplier": "SUP-1", "company": "CO-1",
                    "docstatus": 1, "modified": "2026-10-06 09:00:00", "currency": "GBP",
                    "transaction_date": "2026-10-01", "items": [{"name": "POI-1", "item_code": "COMP-001",
                    "qty": 10000, "received_qty": 8000, "warehouse": "Stores - CO", "uom": "Nos",
                    "stock_uom": "Nos", "conversion_factor": 1, "stock_qty": 10000}]}
        marker = "TAC-SHIP:" + hashlib.sha256(b"approval-ship-1").hexdigest()
        service = {"doctype": "Purchase Order", "name": "PO-SERVICE", "supplier": "SUP-1",
                   "company": "CO-1", "docstatus": 0,
                   "modified": "2026-10-06 09:01:00", "currency": "GBP", "transaction_date": "2026-10-06",
                   "grand_total": 750, "custom_tac_approval_marker": marker,
                   "terms": "Expedited quote Q-1; shipment SHIP-1; original purchase order PO-COMPONENT; priority air",
                   "items": [{"doctype": "Purchase Order Item", "name": "POI-SVC", "parent": "PO-SERVICE",
                              "parenttype": "Purchase Order", "parentfield": "items", "idx": 1,
                              "item_code": "EXP-SHIP-SVC", "qty": 1, "rate": 750,
                              "amount": 750, "schedule_date": "2026-10-19"}]}
        found = {"service": False}
        created_payloads = []
        submit_attempts = 0

        def reader(method, url, kwargs):
            if url.endswith("/Purchase%20Order/PO-COMPONENT"):
                return Response({"data": original})
            if url.endswith("/Purchase%20Order/PO-SERVICE"):
                return Response({"data": service})
            if url.endswith("/Purchase%20Order"):
                filters = json.loads(kwargs["params"]["filters"])
                self.assertEqual(filters, [["custom_tac_approval_marker", "=", marker]])
                if found["service"]:
                    return Response({"data": [{"name": "PO-SERVICE"}]})
                return Response({"data": []})
            self.fail(f"Unexpected read request: {method} {url}")

        def writer(method, url, kwargs):
            if method == "GET" and url.endswith("/Purchase%20Order/PO-SERVICE"):
                return Response({"data": service})
            if method == "POST" and url.endswith("/api/v1/resource/Purchase%20Order"):
                created_payloads.append(kwargs["json"])
                found["service"] = True
                service.update({"docstatus": 0, "transaction_date": kwargs["json"]["transaction_date"],
                               "custom_tac_approval_marker": kwargs["json"]["custom_tac_approval_marker"],
                               "terms": kwargs["json"]["terms"], "items": [{
                    "doctype": "Purchase Order Item", "name": "POI-SVC", "parent": "PO-SERVICE",
                    "parenttype": "Purchase Order", "parentfield": "items", "idx": 1,
                    **kwargs["json"]["items"][0], "amount": 750,
                }]})
                return Response({"data": {"name": "PO-SERVICE"}})
            if method == "POST" and url.endswith("/api/method/frappe.client.submit"):
                nonlocal submit_attempts
                submitted_document = json.loads(kwargs["data"]["doc"])
                self.assertEqual(submitted_document["doctype"], "Purchase Order")
                self.assertEqual(submitted_document["name"], "PO-SERVICE")
                self.assertEqual(submitted_document["company"], "CO-1")
                self.assertEqual(submitted_document["supplier"], "SUP-1")
                self.assertEqual(submitted_document["custom_tac_approval_marker"], marker)
                self.assertEqual(submitted_document["items"][0]["name"], "POI-SVC")
                self.assertEqual(submitted_document["items"][0]["parentfield"], "items")
                submit_attempts += 1
                if submit_attempts == 1:
                    raise requests.ConnectionError("simulated lost submit response")
                service["docstatus"] = 1
                return Response({"message": {"name": "PO-SERVICE"}})
            self.fail(f"Unexpected write request: {method} {url}")

        approval = VerifiedShippingApproval(
            approval_id="approval-ship-1", run_id=run_id, action="expedited_shipping",
            purchase_order_id="PO-COMPONENT", expected_modified=original["modified"],
            policy_version="finance-manager-fixture-v1", quote_id="Q-1", shipment_id="SHIP-1",
            supplier="SUP-1", amount=Decimal("750"), currency="GBP", service="priority air",
            delivery_commitment="2026-10-19", posting_date="2026-10-06", posting_time="09:00:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "idempotency.sqlite"
            adapter = ERPNextAdapter("http://erp.test", "key", "secret", ledger,
                                     site, session=ScriptedHTTP(reader))
            adapter.shipping_session = ScriptedHTTP(writer)
            with self.assertRaises(ERPNextError):
                adapter.commit_expedited_shipping(approval, "run-1:quote-approval")
            # The create succeeded but submit's response was lost, leaving a
            # provider draft. Recovery must reload and submit the full document.
            original["modified"] = "2026-10-07 09:00:00"
            first = adapter.commit_expedited_shipping(approval, "run-1:quote-approval")
            with sqlite3.connect(ledger) as connection:
                connection.execute("DELETE FROM approved_shipping_orders")
            # An uncertain local commit may also need to recover an already
            # submitted native order after the original PO version advances.
            replay = adapter.commit_expedited_shipping(approval, "run-1:quote-approval")

        self.assertEqual(first["docstatus"], 1)
        self.assertEqual(first["name"], replay["name"])
        self.assertEqual(first["fee_amount"], "750")
        self.assertEqual(first["fee_currency"], "GBP")
        self.assertEqual(first["quote_id"], "Q-1")
        self.assertEqual(first["shipment_id"], "SHIP-1")
        self.assertEqual(first["grand_total"], 750)
        self.assertEqual(len(created_payloads), 1)
        self.assertEqual(created_payloads[0]["items"][0]["item_code"], "EXP-SHIP-SVC")
        self.assertEqual(created_payloads[0]["items"][0]["rate"], "750")
        self.assertEqual(created_payloads[0]["custom_tac_approval_marker"], marker)
        self.assertIn("quote Q-1", created_payloads[0]["terms"])
        self.assertNotIn("remarks", created_payloads[0])
        self.assertEqual(submit_attempts, 2)
        self.assertEqual(original["items"][0]["qty"], 10000)

    def test_review_action_is_pre_submit_and_idempotent(self):
        current_state = {"workflow_state": "Draft", "docstatus": 0}
        apply_calls = []

        def response(method, url, kwargs):
            if url.endswith("/Purchase%20Invoice/PINV-1"):
                return Response({"data": {"name": "PINV-1", **current_state}})
            if url.endswith("/frappe.model.workflow.apply_workflow"):
                apply_calls.append(kwargs["data"])
                self.assertEqual(kwargs["data"]["action"], "Request Review")
                current_state.update(workflow_state="Review Requested", docstatus=0)
                return Response({"message": {"name": "PINV-1"}})
            self.fail(f"Unexpected provider request: {method} {url}")

        with tempfile.TemporaryDirectory() as directory:
            http = ScriptedHTTP(response)
            adapter = self.make_adapter(http, directory)
            first = adapter.request_invoice_review("PINV-1", "run-7:review-1")
            replay = adapter.request_invoice_review("PINV-1", "run-7:review-1")

        self.assertEqual(first["workflow_state"], "Review Requested")
        self.assertEqual(replay["docstatus"], 0)
        self.assertEqual(len(apply_calls), 1)
        self.assertEqual(json.loads(apply_calls[0]["doc"]), {"doctype": "Purchase Invoice", "name": "PINV-1"})

    def test_submitted_invoice_cannot_enter_review(self):
        http = ScriptedHTTP(lambda method, url, kwargs: Response({"data": {
            "name": "PINV-1", "workflow_state": "Submitted", "docstatus": 1,
        }}))
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make_adapter(http, directory)
            with self.assertRaises(ERPNextConflict):
                adapter.request_invoice_review("PINV-1", "run-7:review-1")
        self.assertEqual(len(http.calls), 1)

    def test_provider_error_keeps_safe_status_and_original_cause(self):
        http = ScriptedHTTP(lambda method, url, kwargs: Response({"message": "contains private content"}, 503))
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make_adapter(http, directory)
            with self.assertRaises(ERPNextError) as raised:
                adapter.get_purchase_invoice("PINV-1")
        self.assertEqual(raised.exception.status, 503)
        self.assertIsInstance(raised.exception.__cause__, requests.HTTPError)
        self.assertNotIn("private content", str(raised.exception))
        self.assertEqual(http.headers["Authorization"], "token key:secret")
        self.assertEqual(http.headers["Host"], "erp-sim-0123456789abcdef0123456789abcdef.localhost")

    def test_site_name_is_limited_to_owned_simulation_sites(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                ERPNextAdapter("http://erp.test", "key", "secret", Path(directory) / "db",
                               "erp-sim-A1.localhost")

    def test_purchase_receipt_normalizes_native_time_and_rejects_malformed_value(self):
        payload = {"name": "PR-1", "posting_date": "2026-10-06", "posting_time": "1:43:27.80816",
                   "docstatus": 1, "items": []}
        http = ScriptedHTTP(lambda method, url, kwargs: Response({"data": payload}))
        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make_adapter(http, directory)
            for native, expected in (
                ("1:43:27.80816", "01:43:27.808160"),
                ("21:24:1.858301", "21:24:01.858301"),
                ("1:2:3", "01:02:03"),
            ):
                with self.subTest(native=native):
                    payload["posting_time"] = native
                    receipt = adapter.get_purchase_receipt("PR-1")
                    self.assertEqual(receipt["posting_time"], expected)
            payload["posting_time"] = "not-a-time"
            with self.assertRaises(ERPNextError) as raised:
                adapter.get_purchase_receipt("PR-1")
        self.assertIsInstance(raised.exception.__cause__, ValueError)

    def test_site_admin_cli_is_uuid_scoped_and_uses_argument_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            admin = ERPNextSiteAdmin(Path(directory) / "compose.yml")
            with patch("servers.simulation.erpnext.subprocess.run") as run:
                run.return_value.stdout = '{"site_name":"erp-sim-0123456789abcdef0123456789abcdef.localhost"}'
                result = admin.create("01234567-89ab-cdef-0123-456789abcdef")
                admin.ensure_contract("01234567-89ab-cdef-0123-456789abcdef")
            self.assertEqual(result["site_name"], "erp-sim-0123456789abcdef0123456789abcdef.localhost")
            args, kwargs = run.call_args
            self.assertEqual(args[0][-2:], ["ensure-erp-contract", "01234567-89ab-cdef-0123-456789abcdef"])
            self.assertTrue(kwargs["check"])
            with self.assertRaises(ValueError):
                admin.reset("../../other-run")

    def test_native_finance_source_transport_passes_private_payload_and_returns_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory) / "runtime"
            admin = ERPNextSiteAdmin(Path(directory) / "compose.yml", runtime_dir)
            transport = admin.finance_source_adapter()
            seen = []

            def invoke(args, **kwargs):
                operation, operation_id = args[-2:]
                manifest = runtime_dir / "finance-sources" / f"{operation_id}.json"
                self.assertTrue(manifest.is_file())
                self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
                payload = json.loads(manifest.read_text())
                self.assertEqual(args[-2], operation)
                self.assertNotIn("source-order-1", " ".join(args))
                seen.append((operation, payload))
                if operation == "reconcile-finance-sources":
                    result = {"as_of": payload["as_of"], "receipts": [{
                        "source_id": "source-order-1", "doctype": "Purchase Order",
                        "name": "PO-1", "created": True,
                    }]}
                else:
                    result = [{"doctype": "Purchase Invoice", "name": "PI-1", "outstanding_amount": 25}]
                return type("Completed", (), {"stdout": json.dumps({
                    "operation_id": operation_id, "operation": operation, "result": result,
                })})()

            with patch("servers.simulation.erpnext.subprocess.run", side_effect=invoke):
                receipt = transport.reconcile(
                    [{"id": "source-order-1", "source": "sakanaai-coffeebench-native-economy"}],
                    {"company_id": "company", "erp_company": "The Agent Company",
                     "company_business_ids": ["roaster_A"],
                     "counterparty_mail_directory": {"farmer_A": {
                         "actor_id": "coffee_farmer_A", "email": "coffee-farmer_A@example.com",
                         "display_name": "CoffeeBench Farmer A", "kind": "counterparty",
                     }}},
                    "2026-10-30",
                )
                obligations = transport.read_due_obligations("The Agent Company", "2026-10-30")
            self.assertEqual(receipt["receipts"][0]["name"], "PO-1")
            self.assertEqual(obligations[0]["outstanding_amount"], 25)
            self.assertEqual([operation for operation, _payload in seen], [
                "reconcile-finance-sources", "read-due-obligations",
            ])
            self.assertFalse(any((runtime_dir / "finance-sources").glob("*.json")))

    def test_native_evaluation_capture_uses_private_refs_and_versions_only_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory) / "runtime"
            admin = ERPNextSiteAdmin(Path(directory) / "compose.yml", runtime_dir)
            seen = []

            def invoke(args, **kwargs):
                operation, operation_id = args[-2:]
                payload = json.loads((runtime_dir / "finance-sources" / f"{operation_id}.json").read_text())
                assert operation == "capture-evaluation-evidence"
                assert "PO-PRIVATE" not in " ".join(args)
                seen.append(payload)
                evidence = {"schema_version": 1, "company": "The Agent Company", "documents": {}}
                version = {"schema_version": 1, "sha256": "f" * 64}
                result = {"native_evidence": version if payload["versions_only"] else evidence,
                          "version_manifest": version, "retail_observation": None}
                return type("Completed", (), {"stdout": json.dumps({
                    "operation_id": operation_id, "operation": operation, "result": result,
                })})()

            with patch("servers.simulation.erpnext.subprocess.run", side_effect=invoke):
                captured = admin.capture_company_evaluation_evidence(
                    [{"doctype": "Sales Order", "name": "SO-PRIVATE"}], versions_only=True,
                )
            self.assertEqual(captured["version_manifest"]["sha256"], "f" * 64)
            self.assertEqual(seen[0]["entity_refs"], [{"doctype": "Sales Order", "name": "SO-PRIVATE"}])
            self.assertTrue(seen[0]["versions_only"])
            self.assertEqual(seen[0]["erp_company"], "The Agent Company")
            self.assertFalse(any((runtime_dir / "finance-sources").glob("*.json")))

    def test_approved_receipt_uses_native_submit_and_replays_same_document(self):
        run_id = "01234567-89ab-cdef-0123-456789abcdef"
        site = "erp-sim-0123456789abcdef0123456789abcdef.localhost"
        order = {"name": "PO-1", "docstatus": 1, "modified": "2026-10-29 10:00:00",
                 "supplier": "SUP-1", "company": "CO-1", "transaction_date": "2026-10-01",
                 "items": [{"name": "POI-1", "item_code": "ITEM-1", "qty": 2000,
                            "received_qty": 0, "warehouse": "Stores - CO", "rate": 5,
                            "schedule_date": "2026-10-06"}]}
        receipt = {"doctype": "Purchase Receipt", "name": "PR-1", "docstatus": 0,
                   "supplier": "SUP-1", "company": "CO-1", "posting_date": "2026-10-29",
                   "posting_time": "09:00:00", "remarks": "TAC-GR:marker", "items": [{
            "doctype": "Purchase Receipt Item", "name": "PRI-1", "parent": "PR-1",
            "parenttype": "Purchase Receipt", "parentfield": "items", "idx": 1,
            "item_code": "ITEM-1", "purchase_order": "PO-1", "purchase_order_item": "POI-1",
            "received_qty": 2000, "qty": 2000, "warehouse": "Stores - CO",
        }]}
        created = []

        def reader(method, url, kwargs):
            if url.endswith("/Purchase%20Order/PO-1"):
                return Response({"data": order})
            if url.endswith("/Purchase%20Receipt"):
                return Response({"data": [{"name": entry["name"]} for entry in created]})
            if url.endswith("/Purchase%20Receipt/PR-1"):
                return Response({"data": receipt})
            self.fail(f"Unexpected read request: {method} {url}")

        submit_attempts = 0

        def writer(method, url, kwargs):
            if method == "GET" and url.endswith("/Purchase%20Receipt/PR-1"):
                return Response({"data": receipt})
            if method == "POST" and url.endswith("/api/v1/resource/Purchase%20Receipt"):
                self.assertEqual(kwargs["json"]["remarks"].split(":")[0], "TAC-GR")
                self.assertEqual(kwargs["json"]["posting_date"], approval.posting_date)
                self.assertEqual(kwargs["json"]["posting_time"], approval.posting_time)
                created.append({"name": "PR-1"})
                return Response({"data": {"name": "PR-1"}})
            if method == "POST" and url.endswith("/api/method/frappe.client.submit"):
                nonlocal submit_attempts
                submitted_document = json.loads(kwargs["data"]["doc"])
                self.assertEqual(submitted_document["doctype"], "Purchase Receipt")
                self.assertEqual(submitted_document["name"], "PR-1")
                self.assertEqual(submitted_document["supplier"], "SUP-1")
                self.assertEqual(submitted_document["company"], "CO-1")
                self.assertEqual(submitted_document["remarks"], "TAC-GR:marker")
                self.assertEqual(submitted_document["items"][0]["name"], "PRI-1")
                self.assertEqual(submitted_document["items"][0]["parentfield"], "items")
                submit_attempts += 1
                if submit_attempts == 1:
                    raise requests.ConnectionError("simulated lost submit response")
                receipt["docstatus"] = 1
                # Native Frappe time values omit a leading zero in the hour.
                receipt["posting_time"] = "9:00:00"
                return Response({"message": {"name": "PR-1"}})
            self.fail(f"Unexpected writer request: {method} {url}")

        approval = VerifiedERPApproval(
            approval_id="approval-1", run_id=run_id, action="goods_receipt", purchase_order_id="PO-1",
            quantity=Decimal("2000"), expected_modified=order["modified"],
            policy_version="finance-manager-fixture-v1", posting_date="2026-10-29", posting_time="09:00:00",
        )
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "idempotency.sqlite"
            adapter = ERPNextAdapter(
                "http://erp.test", "key", "secret", ledger, site,
                session=ScriptedHTTP(reader), receipt_api_key="poster", receipt_api_secret="poster-secret",
            )
            writer_session = ScriptedHTTP(writer)
            adapter.receipt_session = writer_session
            with self.assertRaises(ERPNextError):
                adapter.create_goods_receipt("PO-1", Decimal("2000"), "run-1:arrival-1", approval)
            # The native draft exists without a local ledger entry. Retry must
            # submit it from a full native document, not the public projection.
            result = adapter.create_goods_receipt("PO-1", Decimal("2000"), "run-1:arrival-1", approval)
            with sqlite3.connect(ledger) as connection:
                connection.execute("DELETE FROM approved_purchase_receipts")
            replay = adapter.create_goods_receipt("PO-1", Decimal("2000"), "run-1:arrival-1", approval)

        self.assertEqual(result["docstatus"], 1)
        self.assertEqual(result["posting_time"], "09:00:00")
        self.assertEqual(replay["name"], "PR-1")
        self.assertEqual(len(created), 1)
        self.assertEqual(submit_attempts, 2)
        self.assertEqual(sum(
            call[0] == "POST" and "/api/v1/resource/Purchase%20Receipt" in call[1]
            for call in writer_session.calls
        ), 1)

    def test_sales_refunds_follow_native_credit_note_and_payment_links(self):
        def response(method, url, kwargs):
            if url.endswith("/Sales%20Order/SO-1"):
                return Response({"data": {"name": "SO-1", "customer": "C-1", "items": []}})
            if url.endswith("/Sales%20Invoice"):
                filters = json.loads(kwargs["params"]["filters"])
                if ["is_return", "=", 0] in filters:
                    return Response({"data": [{"name": "SI-1", "posting_date": "2026-10-01"}]})
                return Response({"data": [{"name": "CN-1", "return_against": "SI-1",
                                             "docstatus": 1, "grand_total": -20, "posting_date": "2026-10-02"}]})
            if url.endswith("/Sales%20Invoice/SI-1"):
                return Response({"data": {"name": "SI-1", "items": [{"sales_order": "SO-1"}]}})
            if url.endswith("/Payment%20Entry"):
                return Response({"data": [{"name": "PE-1"}]})
            if url.endswith("/Payment%20Entry/PE-1"):
                return Response({"data": {"name": "PE-1", "docstatus": 1, "payment_type": "Pay",
                                             "posting_date": "2026-10-03", "references": [
                                                 {"reference_doctype": "Sales Invoice", "reference_name": "SI-1",
                                                  "allocated_amount": 20},
                                             ]}})
            self.fail(f"Unexpected provider request: {method} {url}")

        with tempfile.TemporaryDirectory() as directory:
            adapter = self.make_adapter(ScriptedHTTP(response), directory)
            refunds = adapter.read_sales_invoice_refunds("SO-1")

        self.assertEqual([row["type"] for row in refunds], ["credit_note", "payment_refund"])
        self.assertEqual([row["id"] for row in refunds], ["CN-1", "PE-1"])
        self.assertEqual([row["amount"] for row in refunds], [Decimal("-20"), Decimal("20")])

    def test_approved_receipt_rejects_wrong_run_and_nonexact_outstanding_without_write(self):
        run_id = "01234567-89ab-cdef-0123-456789abcdef"
        site = "erp-sim-0123456789abcdef0123456789abcdef.localhost"
        order = {"name": "PO-1", "docstatus": 1, "modified": "2026-10-29 10:00:00",
                 "supplier": "SUP-1", "company": "CO-1", "transaction_date": "2026-10-01",
                 "items": [{"name": "POI-1", "item_code": "ITEM-1", "qty": 3000,
                            "received_qty": 500, "warehouse": "Stores - CO", "rate": 5}]}
        reader = ScriptedHTTP(lambda method, url, kwargs: (
            Response({"data": []}) if url.endswith("/Purchase%20Receipt")
            else Response({"data": order}) if url.endswith("/Purchase%20Order/PO-1")
            else self.fail(f"Unexpected read request: {method} {url}")
        ))
        writer = ScriptedHTTP(lambda method, url, kwargs: self.fail("Unapproved write was attempted"))

        def approval(**overrides):
            values = {
                "approval_id": "approval-1", "run_id": run_id, "action": "goods_receipt",
                "purchase_order_id": "PO-1", "quantity": Decimal("2000"),
                "expected_modified": order["modified"], "policy_version": "finance-manager-fixture-v1",
                "posting_date": "2026-10-19", "posting_time": "09:00:00",
            }
            values.update(overrides)
            return VerifiedERPApproval(**values)

        with tempfile.TemporaryDirectory() as directory:
            adapter = ERPNextAdapter(
                "http://erp.test", "key", "secret", Path(directory) / "idempotency.sqlite", site,
                session=reader, receipt_api_key="poster", receipt_api_secret="poster-secret",
            )
            adapter.receipt_session = writer
            with self.assertRaises(ERPNextConflict):
                adapter.create_goods_receipt("PO-1", Decimal("2000"), "key-1", approval(run_id="other"))
            with self.assertRaises(ERPNextConflict):
                adapter.create_goods_receipt("PO-1", Decimal("2000"), "key-2", approval())

        self.assertEqual(writer.calls, [])


if __name__ == "__main__":
    unittest.main()

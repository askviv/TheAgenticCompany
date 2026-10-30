"""Permission-checked ERPNext API adapter for the enterprise simulation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


class ERPNextError(RuntimeError):
    """Safe adapter error that retains the provider failure as ``__cause__``."""

    def __init__(self, operation: str, status: int | None = None):
        self.operation = operation
        self.status = status
        detail = f"ERPNext {operation} failed"
        if status is not None:
            detail += f" (HTTP {status})"
        super().__init__(detail)


class ERPNextConflict(ERPNextError):
    """Provider state does not satisfy the requested transition."""


class ERPNextNotFound(ERPNextError):
    """Requested ERPNext record does not exist or is not visible to this user."""


@dataclass(frozen=True)
class VerifiedERPApproval:
    """Trusted engine grant for one approved consequential ERP action."""

    approval_id: str
    run_id: str
    action: str
    purchase_order_id: str
    quantity: Decimal
    expected_modified: str
    policy_version: str
    posting_date: str
    posting_time: str


@dataclass(frozen=True)
class VerifiedShippingApproval:
    """Trusted engine grant for one separate expedited-service purchase order."""

    approval_id: str
    run_id: str
    action: str
    purchase_order_id: str
    expected_modified: str
    policy_version: str
    quote_id: str
    shipment_id: str
    supplier: str
    amount: Decimal
    currency: str
    service: str
    delivery_commitment: str
    posting_date: str
    posting_time: str


class ERPNextAdapter:
    """Read business documents and request a non-posting invoice review transition.

    ``idempotency_path`` must be a private, durable SQLite file shared by all
    processes serving this adapter. Reads/review use a dedicated Frappe API user;
    approved receipts use a separate private poster identity.
    """

    SHIPPING_MARKER_FIELD = "custom_tac_approval_marker"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        idempotency_path: str | Path,
        site_name: str,
        *,
        session: requests.Session | None = None,
        receipt_api_key: str | None = None,
        receipt_api_secret: str | None = None,
        shipping_api_key: str | None = None,
        shipping_api_secret: str | None = None,
        timeout: tuple[float, float] = (3.05, 20.0),
        review_action: str = "Request Review",
        review_state: str = "Review Requested",
    ):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("ERPNext base_url must use HTTP or HTTPS")
        if not api_key or not api_secret:
            raise ValueError("ERPNext API credentials are required")
        if not re.fullmatch(r"erp-sim-[0-9a-f]{32}\.localhost", site_name):
            raise ValueError("site_name must be a coordinator-owned ERP simulation site")
        self.base_url = base_url.rstrip("/")
        self.site_name = site_name
        self.timeout = timeout
        self.review_action = review_action
        self.review_state = review_state
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"token {api_key}:{api_secret}",
            "Accept": "application/json",
            "Host": site_name,
        })
        if bool(receipt_api_key) != bool(receipt_api_secret):
            raise ValueError("both receipt API credentials are required together")
        self.receipt_session: requests.Session | None = None
        if receipt_api_key and receipt_api_secret:
            self.receipt_session = requests.Session()
            self.receipt_session.headers.update({
                "Authorization": f"token {receipt_api_key}:{receipt_api_secret}",
                "Accept": "application/json",
                "Host": site_name,
            })
        if bool(shipping_api_key) != bool(shipping_api_secret):
            raise ValueError("both shipping API credentials are required together")
        self.shipping_session: requests.Session | None = None
        if shipping_api_key and shipping_api_secret:
            self.shipping_session = requests.Session()
            self.shipping_session.headers.update({
                "Authorization": f"token {shipping_api_key}:{shipping_api_secret}",
                "Accept": "application/json",
                "Host": site_name,
            })
        self.idempotency_path = Path(idempotency_path)
        self.idempotency_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.idempotency_path.parent.chmod(0o700)
        self._init_idempotency_store()
        self._review_lock = threading.Lock()

    def _connect_idempotency(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.idempotency_path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _init_idempotency_store(self) -> None:
        with closing(self._connect_idempotency()) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS invoice_review_requests (
                     key_hash TEXT PRIMARY KEY,
                     invoice_id TEXT NOT NULL,
                     workflow_state TEXT NOT NULL
                   )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS approved_purchase_receipts (
                     key_hash TEXT PRIMARY KEY,
                     approval_id TEXT NOT NULL UNIQUE,
                     purchase_order_id TEXT NOT NULL,
                     quantity TEXT NOT NULL,
                     expected_modified TEXT NOT NULL,
                     policy_version TEXT NOT NULL DEFAULT '',
                     posting_date TEXT NOT NULL DEFAULT '',
                     posting_time TEXT NOT NULL DEFAULT '',
                     receipt_id TEXT NOT NULL
                   )"""
            )
            receipt_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(approved_purchase_receipts)")
            }
            for name in ("policy_version", "posting_date", "posting_time"):
                if name not in receipt_columns:
                    connection.execute(
                        f"ALTER TABLE approved_purchase_receipts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
                    receipt_columns.add(name)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS approved_shipping_orders (
                     key_hash TEXT PRIMARY KEY,
                     approval_id TEXT NOT NULL UNIQUE,
                     purchase_order_id TEXT NOT NULL,
                     expected_modified TEXT NOT NULL,
                     quote_id TEXT NOT NULL,
                     shipment_id TEXT NOT NULL,
                     supplier TEXT NOT NULL,
                     amount TEXT NOT NULL,
                     currency TEXT NOT NULL,
                     policy_version TEXT NOT NULL,
                     delivery_commitment TEXT NOT NULL,
                     posting_date TEXT NOT NULL,
                     posting_time TEXT NOT NULL,
                     shipping_order_id TEXT NOT NULL
                   )"""
            )
        if os.name == "posix":
            self.idempotency_path.chmod(0o600)

    @staticmethod
    def _decimal(value: Any, field: str) -> Decimal:
        try:
            return Decimal(str(value if value is not None else "0"))
        except Exception as error:
            raise ValueError(f"ERPNext returned an invalid {field}") from error

    @staticmethod
    def _normalize_native_time(value: Any) -> str:
        try:
            native = str(value)
            pattern = "%H:%M:%S.%f" if "." in native else "%H:%M:%S"
            return datetime.strptime(native, pattern).time().isoformat()
        except (TypeError, ValueError) as error:
            raise ERPNextError("read Purchase Receipt returned an invalid posting time") from error

    def _native_times_equal(self, value: Any, expected: str) -> bool:
        return self._normalize_native_time(value) == time.fromisoformat(expected).isoformat()

    @staticmethod
    def _unwrap(response: requests.Response) -> Any:
        payload = response.json()
        if isinstance(payload, dict):
            if "data" in payload:
                return payload["data"]
            if "message" in payload:
                return payload["message"]
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        request_session: requests.Session | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            response = (request_session or self.session).request(
                method,
                f"{self.base_url}{path}",
                timeout=self.timeout,
                **kwargs,
            )
            if response.status_code == 404:
                raise ERPNextNotFound(operation, response.status_code)
            response.raise_for_status()
            return self._unwrap(response)
        except ERPNextError:
            raise
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            raise ERPNextError(operation, status) from error
        except (requests.RequestException, ValueError) as error:
            raise ERPNextError(operation) from error

    def _get_document(
        self,
        doctype: str,
        name: str,
        *,
        request_session: requests.Session | None = None,
    ) -> dict[str, Any]:
        result = self._request(
            "GET",
            f"/api/v1/resource/{quote(doctype, safe='')}/{quote(name, safe='')}",
            operation=f"read {doctype}",
            request_session=request_session,
        )
        if not isinstance(result, dict):
            raise ERPNextError(f"read {doctype} returned an invalid document")
        return result

    def _submit_native_document(
        self,
        doctype: str,
        name: str,
        *,
        operation: str,
        request_session: requests.Session | None,
    ) -> None:
        # frappe.client.submit constructs a Document from this dict; it does not
        # reload by doctype/name. Fetch the complete native document, including
        # child rows, rather than passing a projection returned by a read method.
        document = self._get_document(doctype, name, request_session=request_session)
        if (document.get("doctype") != doctype or document.get("name") != name
                or not isinstance(document.get("items"), list) or not document["items"]):
            raise ERPNextError(f"read {doctype} returned an incomplete native document for submit")
        self._request(
            "POST",
            "/api/method/frappe.client.submit",
            operation=operation,
            request_session=request_session,
            data={"doc": json.dumps(document)},
        )

    def _get_list(self, doctype: str, filters: list[list[Any]], fields: list[str]) -> list[dict[str, Any]]:
        result = self._request(
            "GET",
            f"/api/v1/resource/{quote(doctype, safe='')}",
            operation=f"list {doctype}",
            params={
                "filters": json.dumps(filters, separators=(",", ":")),
                "fields": json.dumps(fields, separators=(",", ":")),
                "limit_page_length": 500,
            },
        )
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise ERPNextError(f"list {doctype} returned an invalid result")
        return result

    def get_purchase_order(self, purchase_order_id: str) -> dict[str, Any]:
        document = self._get_document("Purchase Order", purchase_order_id)
        return {
            **{key: document.get(key) for key in (
                "name", "supplier", "company", "docstatus", "status", "modified", "transaction_date", "schedule_date",
                "currency", "total", "grand_total", "per_received", "per_billed", "terms",
                self.SHIPPING_MARKER_FIELD,
            )},
            "items": [{key: row.get(key) for key in (
                "name", "item_code", "qty", "received_qty", "billed_amt", "warehouse", "schedule_date", "rate",
                "amount", "description", "uom", "stock_uom", "conversion_factor", "stock_qty",
            )} for row in document.get("items", [])],
        }

    def get_purchase_receipt(self, purchase_receipt_id: str) -> dict[str, Any]:
        document = self._get_document("Purchase Receipt", purchase_receipt_id)
        return {
            **{key: document.get(key) for key in (
                "name", "supplier", "company", "docstatus", "status", "modified", "posting_date",
                "set_warehouse", "total", "base_total", "grand_total", "total_qty", "remarks",
            )},
            "posting_time": self._normalize_native_time(document.get("posting_time")),
            "items": [{key: row.get(key) for key in (
                "name", "item_code", "purchase_order", "purchase_order_item", "received_qty", "qty",
                "rejected_qty", "warehouse", "uom", "stock_uom", "conversion_factor", "stock_qty", "rate",
            )} for row in document.get("items", [])],
        }

    def get_purchase_invoice(self, purchase_invoice_id: str) -> dict[str, Any]:
        document = self._get_document("Purchase Invoice", purchase_invoice_id)
        return {
            **{key: document.get(key) for key in (
                "name", "supplier", "company", "bill_no", "bill_date", "posting_date", "docstatus", "modified",
                "workflow_state", "status", "currency", "grand_total", "paid_amount", "outstanding_amount",
                "update_stock",
            )},
            "items": [{key: row.get(key) for key in (
                "name", "item_code", "qty", "purchase_order", "po_detail", "purchase_receipt", "pr_detail",
                "received_qty",
            )} for row in document.get("items", [])],
        }

    def get_supplier(self, supplier_id: str) -> dict[str, Any]:
        document = self._get_document("Supplier", supplier_id)
        return {key: document.get(key) for key in (
            "name", "supplier_name", "supplier_group", "supplier_type", "country", "disabled",
        )}

    def get_customer(self, customer_id: str) -> dict[str, Any]:
        document = self._get_document("Customer", customer_id)
        return {key: document.get(key) for key in (
            "name", "customer_name", "customer_group", "territory", "customer_type", "disabled",
        )}

    def get_sales_order(self, sales_order_id: str) -> dict[str, Any]:
        document = self._get_document("Sales Order", sales_order_id)
        return {
            **{key: document.get(key) for key in (
                "name", "customer", "company", "docstatus", "status", "modified", "transaction_date",
                "delivery_date", "currency", "grand_total", "per_delivered", "per_billed",
            )},
            "items": [{key: row.get(key) for key in (
                "name", "item_code", "qty", "delivered_qty", "warehouse", "rate",
            )} for row in document.get("items", [])],
        }

    def read_sales_invoice_refunds(self, sales_order_id: str) -> list[dict[str, Any]]:
        order = self.get_sales_order(sales_order_id)
        invoices = self._get_list(
            "Sales Invoice",
            [["customer", "=", order.get("customer")], ["docstatus", "=", 1], ["is_return", "=", 0]],
            ["name", "posting_date", "modified"],
        )
        linked_invoices: dict[str, dict[str, Any]] = {}
        for row in invoices:
            name = str(row.get("name", ""))
            if not name:
                continue
            document = self._get_document("Sales Invoice", name)
            if any(line.get("sales_order") == sales_order_id for line in document.get("items", [])):
                linked_invoices[name] = row

        if not linked_invoices:
            return []

        returns = self._get_list(
            "Sales Invoice",
            [["customer", "=", order.get("customer")], ["docstatus", "=", 1], ["is_return", "=", 1]],
            ["name", "return_against", "docstatus", "grand_total", "posting_date"],
        )
        refunds = [{
            "type": "credit_note",
            "id": row.get("name"),
            "reference_invoice_id": row.get("return_against"),
            "docstatus": int(row.get("docstatus", 0)),
            "amount": self._decimal(row.get("grand_total"), "credit note amount"),
            "date": row.get("posting_date"),
        } for row in returns if row.get("return_against") in linked_invoices]

        payment_entries = self._get_list(
            "Payment Entry",
            [["party_type", "=", "Customer"], ["party", "=", order.get("customer")],
             ["payment_type", "=", "Pay"], ["docstatus", "=", 1]],
            ["name"],
        )
        for row in payment_entries:
            name = str(row.get("name", ""))
            if not name:
                continue
            entry = self._get_document("Payment Entry", name)
            for reference in entry.get("references", []):
                if (reference.get("reference_doctype") == "Sales Invoice"
                        and reference.get("reference_name") in linked_invoices):
                    refunds.append({
                        "type": "payment_refund",
                        "id": name,
                        "reference_invoice_id": reference.get("reference_name"),
                        "docstatus": int(entry.get("docstatus", 0)),
                        "amount": self._decimal(reference.get("allocated_amount"), "refund allocation"),
                        "date": entry.get("posting_date"),
                        "reference_no": entry.get("reference_no"),
                    })
        return sorted(refunds, key=lambda value: (str(value.get("date")), str(value.get("id"))))

    def get_stock_balance(self, item_code: str, warehouse: str) -> Decimal:
        rows = self._get_list(
            "Bin",
            [["item_code", "=", item_code], ["warehouse", "=", warehouse]],
            ["item_code", "warehouse", "actual_qty"],
        )
        if not rows:
            return Decimal(0)
        if len(rows) != 1:
            raise ERPNextNotFound("read stock balance")
        return self._decimal(rows[0].get("actual_qty"), "stock balance")

    def read_payment_status(self, purchase_invoice_id: str) -> dict[str, Any]:
        invoice = self.get_purchase_invoice(purchase_invoice_id)
        payment_entries = self._get_list(
            "Payment Entry",
            [["party_type", "=", "Supplier"], ["party", "=", invoice.get("supplier")],
             ["docstatus", "=", 1]],
            ["name"],
        )
        entries: list[dict[str, Any]] = []
        for payment_entry in payment_entries:
            parent = str(payment_entry.get("name", ""))
            if not parent:
                continue
            entry = self._get_document("Payment Entry", parent)
            if int(entry.get("docstatus", 0)) == 1:
                for row in entry.get("references", []):
                    if row.get("reference_doctype") == "Purchase Invoice" and row.get("reference_name") == purchase_invoice_id:
                        entries.append({
                            "payment_entry_id": parent,
                            "allocated_amount": self._decimal(row.get("allocated_amount"), "allocated amount"),
                            "docstatus": int(entry["docstatus"]),
                            "reference_no": entry.get("reference_no"),
                        })
        return {
            "purchase_invoice_id": purchase_invoice_id,
            "status": invoice.get("status"),
            "docstatus": int(invoice.get("docstatus", 0)),
            "paid_amount": self._decimal(invoice.get("paid_amount"), "paid amount"),
            "outstanding_amount": self._decimal(invoice.get("outstanding_amount"), "outstanding amount"),
            "payments": entries,
        }

    def request_invoice_review(self, invoice_id: str, idempotency_key: str) -> dict[str, Any]:
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        with self._review_lock, closing(self._connect_idempotency()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT invoice_id FROM invoice_review_requests WHERE key_hash = ?", (key_hash,)
            ).fetchone()
            if existing:
                if existing[0] != invoice_id:
                    raise ERPNextConflict("invoice review idempotency key reused for a different invoice")
                invoice = self.get_purchase_invoice(invoice_id)
                if invoice.get("workflow_state") != self.review_state or int(invoice.get("docstatus", 0)) != 0:
                    raise ERPNextConflict("invoice review result no longer matches the recorded request")
                connection.commit()
                return invoice

            invoice = self.get_purchase_invoice(invoice_id)
            if int(invoice.get("docstatus", 0)) != 0:
                raise ERPNextConflict("invoice review is allowed only before invoice submission")
            if invoice.get("workflow_state") == self.review_state:
                connection.execute(
                    "INSERT INTO invoice_review_requests VALUES (?, ?, ?)",
                    (key_hash, invoice_id, self.review_state),
                )
                connection.commit()
                return invoice

            try:
                self._request(
                    "POST",
                    "/api/method/frappe.model.workflow.apply_workflow",
                    operation="request invoice review",
                    data={
                        "doc": json.dumps({"doctype": "Purchase Invoice", "name": invoice_id}),
                        "action": self.review_action,
                    },
                )
                reviewed = self.get_purchase_invoice(invoice_id)
                if int(reviewed.get("docstatus", 0)) != 0 or reviewed.get("workflow_state") != self.review_state:
                    raise ERPNextConflict("invoice workflow did not enter the requested pre-submit review state")
            except Exception:
                connection.rollback()
                raise

            connection.execute(
                "INSERT INTO invoice_review_requests VALUES (?, ?, ?)",
                (key_hash, invoice_id, self.review_state),
            )
            connection.commit()
            return reviewed

    def assess_invoice(self, invoice_id: str) -> dict[str, Any]:
        invoice = self.get_purchase_invoice(invoice_id)
        grouped: dict[str, dict[str, Any]] = {}
        for line in invoice.get("items", []):
            po_row = line.get("po_detail")
            if not po_row:
                continue
            current = grouped.setdefault(str(po_row), {
                "purchase_order_id": line.get("purchase_order"),
                "purchase_order_item_id": str(po_row),
                "item_code": line.get("item_code"),
                "billed_qty": Decimal(0),
                "received_qty": Decimal(0),
                "accepted_qty": Decimal(0),
                "receipt_ids": set(),
            })
            current["billed_qty"] += self._decimal(line.get("qty"), "invoice quantity")

        submitted_receipts = self._get_list(
            "Purchase Receipt",
            [["supplier", "=", invoice.get("supplier")], ["company", "=", invoice.get("company")],
             ["docstatus", "=", 1]],
            ["name"],
        )
        receipt_documents = [
            self.get_purchase_receipt(str(row["name"])) for row in submitted_receipts if row.get("name")
        ]
        for po_row, current in grouped.items():
            purchase_order_id = current["purchase_order_id"]
            for receipt in receipt_documents:
                if int(receipt.get("docstatus", 0)) != 1:
                    continue
                for receipt_line in receipt.get("items", []):
                    if (receipt_line.get("purchase_order") != purchase_order_id
                            or str(receipt_line.get("purchase_order_item")) != po_row):
                        continue
                    current["receipt_ids"].add(str(receipt["name"]))
                    current["received_qty"] += self._decimal(receipt_line.get("received_qty"), "received quantity")
                    current["accepted_qty"] += self._decimal(receipt_line.get("qty"), "accepted quantity")

        rows = []
        for current in grouped.values():
            difference = current["billed_qty"] - current["received_qty"]
            rows.append({
                **{key: value for key, value in current.items() if key != "receipt_ids"},
                "receipt_ids": sorted(current["receipt_ids"]),
                "unreceived_billed_qty": max(difference, Decimal(0)),
                "overbilled": difference > 0,
            })
        return {
            "purchase_invoice_id": invoice_id,
            "docstatus": int(invoice.get("docstatus", 0)),
            "workflow_state": invoice.get("workflow_state"),
            "lines": rows,
            "unlinked_billed_lines": [
                {"item_code": line.get("item_code"), "billed_qty": self._decimal(line.get("qty"), "invoice quantity")}
                for line in invoice.get("items", []) if not line.get("po_detail")
            ],
        }

    def commit_expedited_shipping(
        self, verified_approval: VerifiedShippingApproval, idempotency_key: str,
    ) -> dict[str, Any]:
        """Commit an approved supplier quote as a separate native service Purchase Order."""
        if self.shipping_session is None:
            raise ERPNextConflict("approved shipping writer credentials are not configured")
        if not isinstance(verified_approval, VerifiedShippingApproval):
            raise ERPNextConflict("shipping commitment requires a verified engine approval")
        approval = verified_approval
        amount = self._decimal(approval.amount, "approved shipping amount")
        if amount <= 0 or not idempotency_key or not idempotency_key.strip():
            raise ERPNextConflict("shipping commitment amount and idempotency key are required")
        try:
            if date.fromisoformat(approval.posting_date).isoformat() != approval.posting_date:
                raise ValueError
            if time.fromisoformat(approval.posting_time).isoformat() != approval.posting_time:
                raise ValueError
            if date.fromisoformat(approval.delivery_commitment).isoformat() != approval.delivery_commitment:
                raise ValueError
        except ValueError as error:
            raise ERPNextConflict("verified shipping dates are not canonical ISO format") from error
        run_id = str(uuid.UUID(hex=self.site_name[len("erp-sim-"):-len(".localhost")]))
        if (approval.run_id != run_id or approval.action != "expedited_shipping"
                or not approval.approval_id or not approval.expected_modified or not approval.policy_version
                or not approval.quote_id or not approval.shipment_id or not approval.supplier
                or approval.currency != "GBP" or not approval.service):
            raise ERPNextConflict("verified shipping approval is not bound to this run and quote")

        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        marker = "TAC-SHIP:" + hashlib.sha256(approval.approval_id.encode("utf-8")).hexdigest()
        expected_row = (
            approval.approval_id, approval.purchase_order_id, approval.expected_modified,
            approval.quote_id, approval.shipment_id, approval.supplier, str(amount), approval.currency,
            approval.policy_version, approval.delivery_commitment, approval.posting_date, approval.posting_time,
        )
        with self._review_lock, closing(self._connect_idempotency()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                """SELECT approval_id,purchase_order_id,expected_modified,quote_id,shipment_id,supplier,
                          amount,currency,policy_version,delivery_commitment,posting_date,posting_time,shipping_order_id
                   FROM approved_shipping_orders WHERE key_hash=?""", (key_hash,),
            ).fetchone()
            if prior:
                if prior[:12] != expected_row:
                    raise ERPNextConflict("shipping idempotency key was reused for a different approval")
                original = self.get_purchase_order(approval.purchase_order_id)
                if (int(original.get("docstatus", 0)) != 1 or original.get("supplier") != approval.supplier
                        or original.get("currency") != approval.currency):
                    raise ERPNextConflict("original purchase order no longer matches the shipping approval")
                result = self.get_purchase_order(prior[12])
                result = self._shipping_result(result, approval, amount, expected_company=original.get("company"))
                connection.commit()
                return result
            consumed = connection.execute(
                "SELECT key_hash FROM approved_shipping_orders WHERE approval_id=?", (approval.approval_id,),
            ).fetchone()
            if consumed:
                raise ERPNextConflict("shipping approval has already been consumed")
            matches = self._get_list(
                "Purchase Order", [[self.SHIPPING_MARKER_FIELD, "=", marker]], ["name"],
            )
            if len(matches) > 1:
                raise ERPNextConflict("multiple native service orders match the shipping approval marker")
            original = self.get_purchase_order(approval.purchase_order_id)
            if (int(original.get("docstatus", 0)) != 1 or original.get("supplier") != approval.supplier
                    or original.get("currency") != approval.currency or not original.get("company")):
                raise ERPNextConflict("original purchase order no longer matches the shipping approval")
            if not matches and original.get("modified") != approval.expected_modified:
                raise ERPNextConflict("original purchase order changed after shipping approval")
            if matches:
                service_order = self.get_purchase_order(str(matches[0]["name"]))
                self._shipping_result(service_order, approval, amount, expected_company=original["company"],
                                      allow_draft=True)
                if int(service_order.get("docstatus", 0)) == 0:
                    self._submit_native_document(
                        "Purchase Order", str(service_order["name"]), operation="recover approved shipping order",
                        request_session=self.shipping_session,
                    )
                    service_order = self.get_purchase_order(str(service_order["name"]))
            else:
                created = self._request(
                    "POST", "/api/v1/resource/Purchase%20Order", operation="create approved shipping order",
                    request_session=self.shipping_session,
                    json={
                        "doctype": "Purchase Order", "supplier": approval.supplier,
                        "company": original.get("company"), "currency": approval.currency,
                        "transaction_date": approval.posting_date,
                        "schedule_date": approval.delivery_commitment,
                        self.SHIPPING_MARKER_FIELD: marker,
                        "terms": f"Expedited quote {approval.quote_id}; shipment {approval.shipment_id}; "
                                 f"original purchase order {approval.purchase_order_id}; {approval.service}",
                        "items": [{"item_code": "EXP-SHIP-SVC", "qty": "1", "rate": str(amount),
                                   "schedule_date": approval.delivery_commitment}],
                    },
                )
                if not isinstance(created, dict) or not created.get("name"):
                    raise ERPNextError("create approved shipping order returned no native document name")
                self._submit_native_document(
                    "Purchase Order", str(created["name"]), operation="submit approved shipping order",
                    request_session=self.shipping_session,
                )
                service_order = self.get_purchase_order(str(created["name"]))
            self._shipping_result(service_order, approval, amount, expected_company=original["company"])
            connection.execute(
                """INSERT INTO approved_shipping_orders
                   (key_hash,approval_id,purchase_order_id,expected_modified,quote_id,shipment_id,supplier,
                    amount,currency,policy_version,delivery_commitment,posting_date,posting_time,shipping_order_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key_hash, *expected_row, str(service_order["name"])),
            )
            connection.commit()
            return self._shipping_result(service_order, approval, amount, expected_company=original["company"])

    @staticmethod
    def _shipping_result(
        service_order: dict[str, Any], approval: VerifiedShippingApproval, amount: Decimal,
        *, expected_company: str, allow_draft: bool = False,
    ) -> dict[str, Any]:
        rows = service_order.get("items", [])
        allowed_statuses = (0, 1) if allow_draft else (1,)
        if (int(service_order.get("docstatus", 0)) not in allowed_statuses
                or service_order.get("supplier") != approval.supplier
                or service_order.get("company") != expected_company
                or service_order.get("currency") != approval.currency
                or service_order.get("transaction_date") != approval.posting_date
                or len(rows) != 1 or rows[0].get("item_code") != "EXP-SHIP-SVC"
                or ERPNextAdapter._decimal(rows[0].get("qty"), "shipping service quantity") != Decimal(1)
                or ERPNextAdapter._decimal(rows[0].get("rate"), "shipping service rate") != amount
                or ERPNextAdapter._decimal(service_order.get("grand_total"), "shipping order grand total") != amount
                or rows[0].get("schedule_date") != approval.delivery_commitment
                or service_order.get(ERPNextAdapter.SHIPPING_MARKER_FIELD)
                    != "TAC-SHIP:" + hashlib.sha256(approval.approval_id.encode("utf-8")).hexdigest()
                or approval.quote_id not in str(service_order.get("terms", ""))
                or approval.shipment_id not in str(service_order.get("terms", ""))
                or approval.service not in str(service_order.get("terms", ""))
                or approval.purchase_order_id not in str(service_order.get("terms", ""))):
            raise ERPNextConflict("native shipping order readback does not match the approved quote")
        return {
            **service_order,
            "approval_id": approval.approval_id,
            "quote_id": approval.quote_id,
            "shipment_id": approval.shipment_id,
            "fee_amount": str(amount),
            "fee_currency": approval.currency,
            "service": approval.service,
            "delivery_commitment": approval.delivery_commitment,
        }

    def create_goods_receipt(
        self,
        purchase_order_id: str,
        quantity: Decimal,
        idempotency_key: str,
        verified_approval: VerifiedERPApproval,
    ) -> dict[str, Any]:
        """Create and submit a native Purchase Receipt under a trusted engine grant.

        This method is for the trusted simulation engine only. HTTP tool payloads
        must never construct or accept ``VerifiedERPApproval`` themselves.
        """
        if self.receipt_session is None:
            raise ERPNextConflict("approved receipt writer credentials are not configured")
        if not isinstance(verified_approval, VerifiedERPApproval):
            raise ERPNextConflict("goods receipt requires a verified engine approval")
        requested = self._decimal(quantity, "approved receipt quantity")
        approved = self._decimal(verified_approval.quantity, "approved receipt quantity")
        if requested <= 0 or requested != approved:
            raise ERPNextConflict("goods receipt quantity does not match the verified approval")
        try:
            if date.fromisoformat(verified_approval.posting_date).isoformat() != verified_approval.posting_date:
                raise ValueError
            if time.fromisoformat(verified_approval.posting_time).isoformat() != verified_approval.posting_time:
                raise ValueError
        except ValueError as error:
            raise ERPNextConflict("verified receipt posting date/time is not canonical ISO format") from error
        run_id = str(uuid.UUID(hex=self.site_name[len("erp-sim-"):-len(".localhost")]))
        if (verified_approval.run_id != run_id
                or verified_approval.action != "goods_receipt"
                or verified_approval.purchase_order_id != purchase_order_id
                or not verified_approval.approval_id
                or not verified_approval.expected_modified
                or not verified_approval.policy_version
                or not verified_approval.posting_date
                or not verified_approval.posting_time):
            raise ERPNextConflict("verified approval is not bound to this run and purchase order")
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key is required")

        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        marker = "TAC-GR:" + hashlib.sha256(verified_approval.approval_id.encode("utf-8")).hexdigest()
        with self._review_lock, closing(self._connect_idempotency()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT approval_id, purchase_order_id, quantity, expected_modified, policy_version,
                          posting_date, posting_time, receipt_id
                   FROM approved_purchase_receipts WHERE key_hash = ?""",
                (key_hash,),
            ).fetchone()
            if existing:
                if existing[:7] != (
                    verified_approval.approval_id, purchase_order_id, str(requested),
                    verified_approval.expected_modified, verified_approval.policy_version,
                    verified_approval.posting_date, verified_approval.posting_time,
                ):
                    raise ERPNextConflict("goods receipt idempotency key was reused for a different approval")
                result = self.get_purchase_receipt(existing[7])
                connection.commit()
                return result

            reused = connection.execute(
                "SELECT key_hash FROM approved_purchase_receipts WHERE approval_id = ?",
                (verified_approval.approval_id,),
            ).fetchone()
            if reused:
                raise ERPNextConflict("goods receipt approval has already been consumed")

            matches = self._get_list(
                "Purchase Receipt", [["remarks", "=", marker]], ["name"],
            )
            if len(matches) > 1:
                raise ERPNextConflict("multiple native receipts match the approved operation marker")

            order = self.get_purchase_order(purchase_order_id)
            if int(order.get("docstatus", 0)) != 1:
                raise ERPNextConflict("goods can be received only against a submitted purchase order")
            if order.get("modified") != verified_approval.expected_modified and not matches:
                raise ERPNextConflict("purchase order changed after approval")

            receipt: dict[str, Any] | None = None
            if matches:
                receipt = self.get_purchase_receipt(str(matches[0]["name"]))
                receipt_lines = [row for row in receipt.get("items", [])
                                 if row.get("purchase_order") == purchase_order_id]
                if (len(receipt_lines) != 1
                        or self._decimal(receipt_lines[0].get("received_qty"), "received quantity") != requested
                        or receipt.get("posting_date") != verified_approval.posting_date
                        or not self._native_times_equal(receipt.get("posting_time"), verified_approval.posting_time)):
                    raise ERPNextConflict("existing operation marker does not match the approved receipt")
                source_rows = [row for row in order.get("items", [])
                               if str(row.get("name")) == str(receipt_lines[0].get("purchase_order_item"))]
                if (len(source_rows) != 1
                        or source_rows[0].get("item_code") != receipt_lines[0].get("item_code")
                        or source_rows[0].get("warehouse") != receipt_lines[0].get("warehouse")):
                    raise ERPNextConflict("existing operation marker does not match a native purchase order row")
                if int(receipt.get("docstatus", 0)) == 2:
                    raise ERPNextConflict("approved native receipt was cancelled")
                if int(receipt.get("docstatus", 0)) == 0:
                    self._submit_native_document(
                        "Purchase Receipt", str(receipt["name"]), operation="submit approved goods receipt",
                        request_session=self.receipt_session,
                    )
                    receipt = self.get_purchase_receipt(str(receipt["name"]))
            else:
                open_rows = [
                    row for row in order.get("items", [])
                    if self._decimal(row.get("qty"), "purchase order quantity")
                    - self._decimal(row.get("received_qty"), "received quantity") == requested
                ]
                if len(open_rows) != 1:
                    raise ERPNextConflict("approved receipt does not identify one unambiguous open purchase order row")
                source = open_rows[0]
                warehouse = source.get("warehouse")
                if not warehouse:
                    raise ERPNextConflict("approved purchase order row has no receipt warehouse")
                document = {
                    "doctype": "Purchase Receipt",
                    "supplier": order.get("supplier"),
                    "company": order.get("company"),
                    "posting_date": verified_approval.posting_date,
                    "posting_time": verified_approval.posting_time,
                    "set_posting_time": 1,
                    "remarks": marker,
                    "items": [{
                        "item_code": source.get("item_code"),
                        "qty": str(requested),
                        "received_qty": str(requested),
                        "purchase_order": purchase_order_id,
                        "purchase_order_item": source.get("name"),
                        "warehouse": warehouse,
                        "rate": str(source.get("rate", 0)),
                    }],
                }
                created = self._request(
                    "POST", "/api/v1/resource/Purchase%20Receipt", operation="create approved goods receipt",
                    request_session=self.receipt_session, json=document,
                )
                if not isinstance(created, dict) or not created.get("name"):
                    raise ERPNextError("create approved goods receipt returned no native document name")
                self._submit_native_document(
                    "Purchase Receipt", str(created["name"]), operation="submit approved goods receipt",
                    request_session=self.receipt_session,
                )
                receipt = self.get_purchase_receipt(str(created["name"]))

            if receipt is None or int(receipt.get("docstatus", 0)) != 1:
                raise ERPNextConflict("native goods receipt did not reach submitted state")
            if (receipt.get("posting_date") != verified_approval.posting_date
                    or not self._native_times_equal(receipt.get("posting_time"), verified_approval.posting_time)):
                raise ERPNextConflict("native goods receipt posting date/time does not match the approved arrival")
            receipt_lines = [row for row in receipt.get("items", [])
                             if row.get("purchase_order") == purchase_order_id]
            if len(receipt_lines) != 1:
                raise ERPNextConflict("submitted goods receipt does not match the approved purchase order row")
            source_rows = [row for row in order.get("items", [])
                           if str(row.get("name")) == str(receipt_lines[0].get("purchase_order_item"))]
            if (len(source_rows) != 1
                    or source_rows[0].get("item_code") != receipt_lines[0].get("item_code")
                    or source_rows[0].get("warehouse") != receipt_lines[0].get("warehouse")):
                raise ERPNextConflict("submitted goods receipt does not match a native purchase order row")
            if self._decimal(receipt_lines[0].get("received_qty"), "received quantity") != requested:
                raise ERPNextConflict("submitted goods receipt quantity does not match approval")
            connection.execute(
                """INSERT INTO approved_purchase_receipts
                   (key_hash, approval_id, purchase_order_id, quantity, expected_modified, policy_version,
                    posting_date, posting_time, receipt_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key_hash, verified_approval.approval_id, purchase_order_id, str(requested),
                 verified_approval.expected_modified, verified_approval.policy_version,
                 verified_approval.posting_date, verified_approval.posting_time, str(receipt["name"])),
            )
            connection.commit()
            return receipt


class ERPNextSiteAdmin:
    """Trusted operator CLI bridge to owned ERP sites; never mount Docker into an API."""

    def __init__(self, compose_file: str | Path, runtime_dir: str | Path | None = None):
        self.compose_file = Path(compose_file)
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else None

    @staticmethod
    def _run_id(run_id: str) -> str:
        try:
            parsed = uuid.UUID(run_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("run_id must be a canonical UUID") from error
        if str(parsed) != run_id.lower():
            raise ValueError("run_id must be a canonical UUID")
        return str(parsed)

    def _execute(self, operation: str, run_id: str) -> dict[str, Any]:
        canonical_id = self._run_id(run_id)
        command = [
            "docker", "compose", "-f", str(self.compose_file), "--profile", "admin",
            "run", "--rm", "site-admin", operation, canonical_id,
        ]
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
        except subprocess.CalledProcessError as error:
            raise ERPNextError(f"{operation} owned ERP site") from error
        except subprocess.TimeoutExpired as error:
            raise ERPNextError(f"{operation} owned ERP site timed out") from error
        except OSError as error:
            raise ERPNextError(f"{operation} owned ERP site") from error
        try:
            result = json.loads(completed.stdout)
        except ValueError as error:
            raise ERPNextError(f"{operation} owned ERP site returned invalid output") from error
        if not isinstance(result, dict):
            raise ERPNextError(f"{operation} owned ERP site returned invalid output")
        return result

    def create(self, run_id: str) -> dict[str, Any]:
        return self._execute("create", run_id)

    def reset(self, run_id: str) -> dict[str, Any]:
        return self._execute("reset", run_id)

    def ensure_contract(self, run_id: str) -> dict[str, Any]:
        return self._execute("ensure-erp-contract", run_id)

    def _finance_operation(self, operation: str, payload: dict[str, Any]) -> Any:
        if self.runtime_dir is None:
            raise RuntimeError("private ERP runtime volume is required for native company operations")
        if operation not in {"reconcile-finance-sources", "read-due-obligations",
                             "reconcile-company-issues", "company-issue-notifications", "company-evidence-setup", "reconcile-ecommerce-finance",
                             "capture-evaluation-evidence"}:
            raise ValueError("unsupported native company operation")
        operation_id = str(uuid.uuid4())
        directory = self.runtime_dir / "finance-sources"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            directory.chmod(0o700)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{operation_id}.", dir=directory)
        manifest_path = directory / f"{operation_id}.json"
        temporary = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(serialized)
            os.replace(temporary, manifest_path)
            command = [
                "docker", "compose", "-f", str(self.compose_file), "--profile", "admin",
                "run", "--rm", "site-admin", operation, operation_id,
            ]
            try:
                completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
            except subprocess.CalledProcessError as error:
                raise ERPNextError(f"{operation} native company ERPNext") from error
            except subprocess.TimeoutExpired as error:
                raise ERPNextError(f"{operation} native company ERPNext timed out") from error
            except OSError as error:
                raise ERPNextError(f"{operation} native company ERPNext") from error
            try:
                response = json.loads(completed.stdout)
            except ValueError as error:
                raise ERPNextError(f"{operation} native company ERPNext returned invalid output") from error
            if (not isinstance(response, dict) or response.get("operation_id") != operation_id
                    or response.get("operation") != operation or "result" not in response):
                raise ERPNextError(f"{operation} native company ERPNext returned an invalid binding")
            return response["result"]
        finally:
            temporary.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)

    def reconcile_finance_sources(
        self, source_records: list[dict[str, Any]], company_binding: dict[str, Any], as_of: str | datetime,
    ) -> dict[str, Any]:
        boundary = as_of.isoformat() if isinstance(as_of, datetime) else as_of
        result = self._finance_operation("reconcile-finance-sources", {
            "source_records": source_records, "company_binding": company_binding, "as_of": boundary,
        })
        if (not isinstance(result, dict) or not isinstance(result.get("receipts"), list)
                or not isinstance(result.get("as_of"), str)):
            raise ERPNextError("reconcile-finance-sources returned an invalid receipt")
        return result

    def read_due_obligations(self, erp_company: str, as_of: str | datetime) -> list[dict[str, Any]]:
        boundary = as_of.isoformat() if isinstance(as_of, datetime) else as_of
        result = self._finance_operation("read-due-obligations", {
            "erp_company": erp_company, "as_of": boundary,
        })
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise ERPNextError("read-due-obligations returned an invalid result")
        return result

    def finance_source_adapter(self):
        from .finance_sources import NativeFinanceSourceAdapter

        return NativeFinanceSourceAdapter(self.reconcile_finance_sources, self.read_due_obligations)

    def reconcile_company_issues(self, events: list[dict], company_binding: dict) -> dict:
        return self._finance_operation("reconcile-company-issues", {
            "events": events, "company_binding": company_binding,
        })

    def reconcile_ecommerce_finance(self, records: list[dict], binding: dict, as_of: str) -> dict:
        return self._finance_operation("reconcile-ecommerce-finance", {
            "records": records, "company_binding": binding, "as_of": as_of,
        })

    def company_evidence_setup(self, endpoint: str, secret: str, cursor: dict | None = None) -> dict:
        return self._finance_operation("company-evidence-setup", {
            "erp_company": "The Agent Company", "endpoint": endpoint, "secret": secret, "cursor": cursor,
        })

    def company_issue_notifications(self) -> dict:
        return self._finance_operation("company-issue-notifications", {"erp_company": "The Agent Company"})

    def capture_company_evaluation_evidence(
        self, entity_refs: list[dict[str, str]], *, task_id: str | None = None,
        experiment_id: str | None = None, versions_only: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "erp_company": "The Agent Company", "entity_refs": entity_refs,
            "versions_only": versions_only,
        }
        if task_id is not None or experiment_id is not None:
            if not task_id or not experiment_id:
                raise ValueError("τ evaluation evidence requires both task_id and experiment_id")
            payload.update(task_id=task_id, experiment_id=self._run_id(experiment_id))
        result = self._finance_operation("capture-evaluation-evidence", payload)
        if (not isinstance(result, dict) or not isinstance(result.get("native_evidence"), dict)
                or not isinstance(result.get("version_manifest"), dict)
                or (task_id is not None and not isinstance(result.get("retail_observation"), dict))):
            raise ERPNextError("capture-evaluation-evidence returned an invalid result")
        return result


class ERPNextRetailAdmin:
    """Trusted bridge that imports a task slice into the existing company site."""

    def __init__(self, compose_file: str | Path, runtime_dir: str | Path,
                 *, operator_users: list[str] | None = None):
        self.compose_file = Path(compose_file)
        self.runtime_dir = Path(runtime_dir)
        self.operator_users = list(operator_users or [])

    @staticmethod
    def _canonical_experiment_id(experiment_id: str) -> str:
        try:
            parsed = uuid.UUID(experiment_id)
        except (ValueError, AttributeError) as error:
            raise ValueError("experiment_id must be a canonical UUID") from error
        if str(parsed) != experiment_id.lower():
            raise ValueError("experiment_id must be a canonical UUID")
        return str(parsed)

    @staticmethod
    def _task_id(task_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ValueError("task_id contains unsupported characters")
        return task_id

    def seed_task(
        self,
        experiment_id: str,
        task_id: str,
        manifest: dict[str, Any],
        *,
        projection_as_of: str,
    ) -> dict[str, Any]:
        experiment_id = self._canonical_experiment_id(experiment_id)
        task_id = self._task_id(task_id)
        if not isinstance(manifest, dict) or manifest.get("task_id") != task_id:
            raise ValueError("manifest task_id does not match the requested task")
        integrity = manifest.get("source_integrity")
        if not isinstance(integrity, dict) or integrity.get("status") not in ("valid", "failed"):
            raise ValueError("τ retail manifest source-integrity report is missing or invalid")
        issues = integrity.get("issues")
        if not isinstance(issues, list) or any(not isinstance(issue, dict) for issue in issues):
            raise ValueError("τ retail manifest source-integrity details are invalid")
        if integrity["status"] == "valid" and issues:
            raise ValueError("τ retail manifest source-integrity status conflicts with its issues")
        if not isinstance(projection_as_of, str):
            raise ValueError("τ retail native posting requires a projection_as_of date")
        try:
            if date.fromisoformat(projection_as_of).isoformat() != projection_as_of:
                raise ValueError
        except ValueError as error:
            raise ValueError("projection_as_of must be YYYY-MM-DD") from error
        seed_manifest = {**manifest, "projection_as_of": projection_as_of,
                         "retail_operator_users": self.operator_users}
        directory = self.runtime_dir / "tau-retail" / experiment_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            directory.chmod(0o700)
        manifest_path = directory / f"{task_id}.json"
        encoded = json.dumps(seed_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        if manifest_path.exists():
            if manifest_path.read_text(encoding="utf-8") != encoded:
                raise FileExistsError("Private τ retail manifest already exists with different contents")
        else:
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{task_id}.", dir=directory)
            temporary = Path(temporary_name)
            try:
                if os.name == "posix":
                    os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(encoded)
                os.replace(temporary, manifest_path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
        command = [
            "docker", "compose", "-f", str(self.compose_file), "--profile", "admin",
            "run", "--rm", "site-admin", "seed-tau-retail", experiment_id, task_id,
        ]
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
        except subprocess.CalledProcessError as error:
            raise ERPNextError("seed τ retail task in company ERPNext") from error
        except subprocess.TimeoutExpired as error:
            raise ERPNextError("seed τ retail task in company ERPNext timed out") from error
        except OSError as error:
            raise ERPNextError("seed τ retail task in company ERPNext") from error
        try:
            result = json.loads(completed.stdout)
        except ValueError as error:
            raise ERPNextError("seed τ retail task returned invalid output") from error
        if (not isinstance(result, dict) or result.get("task_id") != task_id
                or result.get("experiment_id") != experiment_id or result.get("company") != "The Agent Company"):
            raise ERPNextError("seed τ retail task returned an invalid binding")
        return result

    def observe_task(self, manifest: dict[str, Any], experiment_id: str) -> dict[str, Any]:
        experiment_id = self._canonical_experiment_id(experiment_id)
        task_id = self._task_id(manifest.get("task_id") if isinstance(manifest, dict) else "")
        if not isinstance(manifest, dict) or manifest.get("task_id") != task_id:
            raise ValueError("manifest task_id is invalid")
        directory = self.runtime_dir / "tau-retail" / experiment_id
        manifest_path = directory / f"{task_id}.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("Private τ retail task manifest is unavailable for observation")
        command = ["docker", "compose", "-f", str(self.compose_file), "--profile", "admin",
                   "run", "--rm", "site-admin", "observe-tau-retail", experiment_id, task_id]
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=900)
        except subprocess.CalledProcessError as error:
            raise ERPNextError("observe τ retail task in company ERPNext") from error
        except subprocess.TimeoutExpired as error:
            raise ERPNextError("observe τ retail task in company ERPNext timed out") from error
        except OSError as error:
            raise ERPNextError("observe τ retail task in company ERPNext") from error
        try:
            result = json.loads(completed.stdout)
        except ValueError as error:
            raise ERPNextError("observe τ retail task returned invalid output") from error
        if (not isinstance(result, dict) or result.get("task_id") != task_id
                or result.get("experiment_id") != experiment_id or not isinstance(result.get("observation"), dict)
                or not isinstance(result.get("unmapped"), list)):
            raise ERPNextError("observe τ retail task returned an invalid binding")
        return result

    def capture_evaluation_evidence(
        self, manifest: dict[str, Any], experiment_id: str, import_receipt: dict[str, Any],
        *, versions_only: bool = False,
    ) -> dict[str, Any]:
        experiment_id = self._canonical_experiment_id(experiment_id)
        task_id = self._task_id(manifest.get("task_id") if isinstance(manifest, dict) else "")
        if (not isinstance(manifest, dict) or manifest.get("task_id") != task_id
                or not isinstance(import_receipt, dict) or import_receipt.get("task_id") != task_id
                or import_receipt.get("experiment_id") != experiment_id):
            raise ValueError("τ evaluation evidence inputs must bind the imported task and experiment")
        native_documents = import_receipt.get("native_documents")
        if not isinstance(native_documents, dict):
            raise ValueError("τ retail import receipt has no native document mapping")
        doctype_by_entity = {
            "users": "Customer", "products": "Item", "items": "Item",
            "item_prices": "Item Price",
            "orders": "Sales Order", "payment_methods": "Mode of Payment",
            "addresses": "Address", "service_requests": "Issue",
        }
        refs = set()
        for entity, doctype in doctype_by_entity.items():
            names = native_documents.get(entity, {})
            if not isinstance(names, dict):
                raise ValueError(f"τ retail native document mapping for {entity} is invalid")
            refs.update((doctype, name) for name in names.values() if isinstance(name, str) and name)
        if not refs:
            raise ValueError("τ retail import receipt contains no native evaluation documents")
        result = ERPNextSiteAdmin(self.compose_file, self.runtime_dir).capture_company_evaluation_evidence(
            [{"doctype": doctype, "name": name} for doctype, name in sorted(refs)],
            task_id=None if versions_only else task_id,
            experiment_id=None if versions_only else experiment_id,
            versions_only=versions_only,
        )
        if versions_only:
            return {"version_manifest": result["version_manifest"]}
        retail = result["retail_observation"]
        if (retail.get("task_id") != task_id or retail.get("experiment_id") != experiment_id
                or not isinstance(retail.get("observation"), dict)
                or not isinstance(retail.get("unmapped"), list)):
            raise ERPNextError("capture-evaluation-evidence returned an invalid τ projection")
        return {
            "task_id": task_id, "experiment_id": experiment_id,
            "observation": retail["observation"], "unmapped": retail["unmapped"],
            "source_gaps": retail.get("source_gaps", []),
            "native_evidence": result["native_evidence"],
            "version_manifest": result["version_manifest"],
        }

    def evaluation_version_manifest(
        self, manifest: dict[str, Any], experiment_id: str, import_receipt: dict[str, Any],
    ) -> dict[str, Any]:
        return self.capture_evaluation_evidence(
            manifest, experiment_id, import_receipt, versions_only=True,
        )["version_manifest"]

"""Translate explicit native ERP evidence to confirmed τ retail actions.

The upstream τ database remains authoritative for criteria and the observed
ERP projection remains authoritative for current state. This adapter emits an
action only where the native transition and its source fields establish the
same τ operation; requests and draft financial documents are not treated as
completed returns or payments.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from servers.simulation.tau2_retail import UPSTREAM_COMMIT
from servers.simulation.feedback_contracts import (
    NativeActionReceipt,
    TranslationResult,
    TranslatedAction,
    read_evidence_scalar,
)


_CANCEL_REASONS = {"no longer needed", "ordered by mistake"}
_PENDING_STATES = {"Pending", "Pending (Item Modified)"}


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _plain(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return value
    raise TypeError("τ retail feedback inputs must be contract models or mappings")


def _result(
    status: str,
    reason: str,
    *,
    action: dict[str, Any] | None = None,
    coverage: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "action": action,
        "reason": reason,
        "coverage": coverage or [],
    }


def _translated_action(
    mapping: dict[str, Any],
    evidence: dict[str, Any],
    native_action: str,
    arguments: dict[str, Any],
    coverage: list[str],
) -> dict[str, Any]:
    mapping_id, evidence_id = mapping.get("mapping_id"), evidence.get("evidence_id")
    company_time = evidence.get("native_observed_company_time") or evidence.get(
        "company_time"
    )
    if (
        not isinstance(mapping_id, str)
        or not mapping_id
        or not isinstance(evidence_id, str)
        or not evidence_id
    ):
        return _result("inconclusive", "mapping and evidence require stable IDs")
    if not isinstance(company_time, str) or not company_time:
        return _result("inconclusive", "native evidence has no company-clock timestamp")
    action_id = hashlib.sha256(
        f"{mapping_id}:{evidence_id}".encode("utf-8")
    ).hexdigest()
    translated = {
        "action_id": action_id,
        "mapping_id": mapping_id,
        "evidence_id": evidence_id,
        "benchmark": "tau_retail",
        "native_action": native_action,
        "arguments": arguments,
        "company_time": company_time,
        "supporting_evidence": evidence.get("supporting_evidence", []),
    }
    return _result(
        "supported",
        "translated through an explicitly mapped native ERP transition",
        coverage=coverage,
        action=TranslatedAction.model_validate(translated).model_dump(mode="json"),
    )


def _identity(
    mapping: dict[str, Any], evidence: dict[str, Any]
) -> dict[str, Any] | None:
    if (
        mapping.get("company_id") != "company"
        or evidence.get("company_id") != "company"
    ):
        return _result("unmapped", "mapping and evidence are outside the company scope")
    if (
        mapping.get("benchmark") != "tau_retail"
        or mapping.get("upstream_commit") != UPSTREAM_COMMIT
    ):
        return _result("unmapped", "mapping does not target the pinned τ retail source")
    if mapping.get("erp_doctype") != evidence.get("erp_doctype") or mapping.get(
        "erp_name"
    ) != evidence.get("erp_name"):
        return _result(
            "unmapped", "evidence does not identify the mapped native ERP document"
        )
    if (
        mapping.get("experiment_id")
        and evidence.get("experiment_id")
        and evidence.get("experiment_id") != mapping["experiment_id"]
    ):
        return _result(
            "inconclusive",
            "evidence belongs to a different task execution for a shared record",
        )
    context = mapping.get("native_context")
    if not isinstance(context, dict):
        return _result(
            "inconclusive", "mapping has no explicit native τ action context"
        )
    if context.get("shared_record_ambiguous"):
        return _result(
            "inconclusive",
            "shared τ record has overlapping task evidence that cannot be attributed",
        )
    if not isinstance(evidence.get("document"), dict):
        return _result("inconclusive", "native evidence has no captured document state")
    return None


def _mapped_fields(
    context: dict[str, Any], evidence: dict[str, Any], expected: set[str]
) -> dict[str, Any] | None:
    native_ids = context.get("native_ids")
    document_fields = context.get("document_fields")
    if not isinstance(native_ids, dict) or not isinstance(document_fields, dict):
        return None
    if set(document_fields) != expected or set(native_ids).intersection(
        document_fields
    ):
        return None
    if any(
        not isinstance(key, str) or not key for key in (*native_ids, *document_fields)
    ):
        return None
    values = dict(native_ids)
    try:
        for argument, path in document_fields.items():
            if not isinstance(path, str) or not path:
                return None
            values[argument] = read_evidence_scalar(evidence, path)
    except (KeyError, ValueError):
        return None
    return values


def _cancel_refund_proof(
    context: dict[str, Any], evidence: dict[str, Any]
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Require submitted ERP payment history to prove τ cancellation refunds."""
    source_order = context.get("source_snapshot")
    native_observation = evidence.get("native_observation")
    observed_order = (
        native_observation.get("order")
        if isinstance(native_observation, dict)
        else None
    )
    observed_user = (
        native_observation.get("user")
        if isinstance(native_observation, dict)
        else None
    )
    references = (
        native_observation.get("native_evidence")
        if isinstance(native_observation, dict)
        else None
    )
    if not isinstance(source_order, dict) or not isinstance(
        source_order.get("payment_history"), list
    ):
        return False, "cancel mapping has no source payment-history facts", []
    if not isinstance(observed_order, dict) or not isinstance(
        observed_order.get("payment_history"), list
    ):
        return False, "current ERP-observed posted payment history is unavailable", []
    if (
        not isinstance(observed_user, dict)
        or observed_user.get("user_id") != source_order.get("user_id")
        or not isinstance(observed_user.get("payment_methods"), dict)
    ):
        return False, "current ERP-observed customer payment methods are unavailable", []
    if not isinstance(references, list):
        return (
            False,
            "current ERP-observed native document references are unavailable",
            [],
        )
    valid_refs = []
    for reference in references:
        if (
            not isinstance(reference, dict)
            or reference.get("doctype") not in ("Sales Order", "Payment Entry")
            or not isinstance(reference.get("name"), str)
            or not reference["name"]
            or not isinstance(reference.get("modified"), str)
            or not reference["modified"]
            or not isinstance(reference.get("docstatus"), int)
        ):
            return False, "native evidence reference is incomplete", []
        valid_refs.append(reference)
    order_id = evidence.get("erp_name")
    order_refs = [ref for ref in valid_refs if ref["doctype"] == "Sales Order"]
    payment_refs = [ref for ref in valid_refs if ref["doctype"] == "Payment Entry"]
    if (
        len(order_refs) != 1
        or order_refs[0]["name"] != order_id
        or order_refs[0]["docstatus"] != 2
    ):
        return (
            False,
            "native cancellation reference does not identify the cancelled Sales Order",
            [],
        )
    if len(payment_refs) != len(observed_order["payment_history"]) or any(
        ref["docstatus"] != 1 for ref in payment_refs
    ):
        return (
            False,
            "posted ERP payment references do not match observed payment history",
            [],
        )
    if len({ref["name"] for ref in valid_refs}) != len(valid_refs):
        return False, "native evidence contains duplicate document references", []
    original = source_order["payment_history"]
    observed = observed_order["payment_history"]
    if not original:
        if observed:
            return (
                False,
                "ERP contains payment transactions for a source order with no original payments",
                [],
            )
        return (
            True,
            "ERP confirms no original payment or refund transaction",
            [{
                "kind": "tau_retail_current_erp_projection",
                "order": observed_order,
                "user": observed_user,
                "source_order": source_order,
                "native_document_refs": valid_refs,
            }],
        )
    expected = []
    for row in original:
        if not isinstance(row, dict) or row.get("transaction_type") not in (
            "payment",
            "refund",
        ):
            return (
                False,
                "source payment history contains an unsupported transaction",
                [],
            )
        try:
            amount = Decimal(str(row.get("amount")))
        except (InvalidOperation, ValueError):
            return False, "source payment history contains an invalid amount", []
        method = row.get("payment_method_id")
        if (
            not amount.is_finite()
            or amount < 0
            or not isinstance(method, str)
            or not method
        ):
            return (
                False,
                "source payment history does not identify an exact amount and method",
                [],
            )
        expected.append(
            {
                "transaction_type": row["transaction_type"],
                "amount": amount,
                "payment_method_id": method,
            }
        )
    expected.extend(
        {
            "transaction_type": "refund",
            "amount": row["amount"],
            "payment_method_id": row["payment_method_id"],
        }
        for row in list(expected)
    )
    actual = []
    for row in observed:
        if not isinstance(row, dict) or row.get("transaction_type") not in (
            "payment",
            "refund",
        ):
            return (
                False,
                "ERP-observed payment history contains an unsupported transaction",
                [],
            )
        try:
            amount = Decimal(str(row.get("amount")))
        except (InvalidOperation, ValueError):
            return False, "ERP-observed payment history contains an invalid amount", []
        method = row.get("payment_method_id")
        if (
            not amount.is_finite()
            or amount < 0
            or not isinstance(method, str)
            or not method
        ):
            return (
                False,
                "ERP-observed payment history lacks an exact posted amount or method",
                [],
            )
        actual.append(
            {
                "transaction_type": row["transaction_type"],
                "amount": amount,
                "payment_method_id": method,
            }
        )
    if actual != expected:
        return (
            False,
            "posted ERP refund history does not exactly match τ cancellation refunds",
            [],
        )
    for row in original:
        method = observed_user["payment_methods"].get(row["payment_method_id"])
        if not isinstance(method, dict) or not isinstance(method.get("source"), str):
            return False, "ERP customer projection does not identify an original payment method", []
        if method["source"] == "gift_card" and not isinstance(method.get("balance"), (int, float)):
            return False, "ERP gift-card projection has no observed balance", []
    return (
        True,
        "posted ERP payment and refund history exactly matches τ cancellation effects",
        [{
            "kind": "tau_retail_current_erp_projection",
            "order": observed_order,
            "user": observed_user,
            "source_order": source_order,
            "native_document_refs": valid_refs,
        }],
    )


def _service_request_arguments(
    mapping: dict[str, Any], evidence: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    context = mapping.get("native_context", {})
    action = context.get("native_action")
    observation = evidence.get("native_observation")
    issue = observation.get("service_issue") if isinstance(observation, dict) else None
    order = observation.get("order") if isinstance(observation, dict) else None
    user = observation.get("user") if isinstance(observation, dict) else None
    native_ids = context.get("native_ids")
    source_order = context.get("source_snapshot")
    document = evidence.get("document")
    if not all(isinstance(value, dict) for value in (issue, order, user, native_ids, source_order, document)):
        return None
    source_id = mapping.get("source_record_id")
    issue_type = "Return" if action == "return_delivered_order_items" else "Exchange"
    projected_status = "return requested" if issue_type == "Return" else "exchange requested"
    expected_fields = {
        "status": "Open",
        "custom_customer_service_type": issue_type,
        "custom_customer_service_order": native_ids.get("sales_order_name"),
    }
    for field, expected in expected_fields.items():
        if issue.get(field) != expected or document.get(field) != expected:
            return None
    if (
        mapping.get("erp_doctype") != "Issue"
        or mapping.get("source_entity_type") != "order"
        or mapping.get("erp_name") != evidence.get("erp_name")
        or issue.get("name") != mapping.get("erp_name")
        or native_ids.get("order_id") != source_id
        or source_order.get("order_id") != source_id
        or source_order.get("status") != "delivered"
        or order.get("order_id") != source_id
        or order.get("user_id") != source_order.get("user_id")
        or order.get("status") != projected_status
        or user.get("user_id") != source_order.get("user_id")
        or not isinstance(user.get("payment_methods"), dict)
    ):
        return None
    order_name = native_ids.get("sales_order_name")
    issue_ref = issue.get("native_ref")
    references = observation.get("native_evidence")
    if not isinstance(references, list) or not isinstance(issue_ref, dict):
        return None
    order_refs = [ref for ref in references if isinstance(ref, dict) and ref.get("doctype") == "Sales Order"]
    if len(order_refs) != 1 or order_refs[0].get("name") != order_name or order_refs[0].get("docstatus") != 1:
        return None
    if (
        issue_ref.get("doctype") != "Issue"
        or issue_ref.get("name") != mapping.get("erp_name")
        or issue_ref.get("docstatus") not in (0, 1)
        or not issue_ref.get("modified")
    ):
        return None
    action_refs = [*order_refs, issue_ref]

    def parsed_items(issue_field: str) -> list[str] | None:
        field_value = issue.get(issue_field)
        document_value = document.get(issue_field)
        try:
            document_items = json.loads(document_value) if isinstance(document_value, str) else document_value
        except (TypeError, ValueError):
            return None
        if not isinstance(field_value, list) or field_value != document_items:
            return None
        if any(not isinstance(item_id, str) or not item_id for item_id in field_value):
            return None
        return field_value

    item_ids = parsed_items("custom_customer_service_item_ids")
    method_id = issue.get("payment_method_id")
    if (
        item_ids is None
        or not isinstance(method_id, str)
        or not method_id
        or method_id not in user["payment_methods"]
        or document.get("custom_customer_service_payment_method")
        != issue.get("custom_customer_service_payment_method")
    ):
        return None
    if action == "return_delivered_order_items":
        if order.get("return_items") != sorted(item_ids) or order.get("return_payment_method_id") != method_id:
            return None
        arguments = {"order_id": source_id, "item_ids": item_ids, "payment_method_id": method_id}
    else:
        new_item_ids = parsed_items("custom_customer_service_new_item_ids")
        if (
            new_item_ids is None
            or order.get("exchange_items") != sorted(item_ids)
            or order.get("exchange_new_items") != sorted(new_item_ids)
            or order.get("exchange_payment_method_id") != method_id
        ):
            return None
        arguments = {
            "order_id": source_id, "item_ids": item_ids,
            "new_item_ids": new_item_ids, "payment_method_id": method_id,
        }
    return arguments, {
        "kind": "tau_retail_current_erp_projection",
        "source_order": source_order,
        "order": order,
        "user": user,
        "service_issue": issue,
        "native_document_refs": action_refs,
    }


def _order_payment_evidence_refs(
    mapping: dict[str, Any], evidence: dict[str, Any], order: dict[str, Any]
) -> list[dict[str, Any]] | None:
    observation = evidence.get("native_observation")
    refs = observation.get("native_evidence") if isinstance(observation, dict) else None
    if not isinstance(refs, list):
        return None
    order_refs = [row for row in refs if isinstance(row, dict) and row.get("doctype") == "Sales Order"]
    payment_refs = [row for row in refs if isinstance(row, dict) and row.get("doctype") == "Payment Entry"]
    if (
        len(order_refs) != 1
        or order_refs[0].get("name") != mapping.get("erp_name")
        or order_refs[0].get("docstatus") != 1
        or not isinstance(order_refs[0].get("modified"), str)
        or not order_refs[0]["modified"]
        or len(payment_refs) != len(order.get("payment_history", []))
        or any(row.get("docstatus") != 1 or not isinstance(row.get("modified"), str) or not row["modified"]
               for row in payment_refs)
        or len({row.get("name") for row in refs if isinstance(row, dict)}) != len(refs)
    ):
        return None
    return refs


def _pending_order_address_projection(
    mapping: dict[str, Any], evidence: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    context = mapping.get("native_context", {})
    source_id = mapping.get("source_record_id")
    observation = evidence.get("native_observation")
    order = observation.get("order") if isinstance(observation, dict) else None
    source_order = context.get("source_snapshot")
    refs = observation.get("native_evidence") if isinstance(observation, dict) else None
    if not all(isinstance(value, dict) for value in (observation, order, source_order)) or not isinstance(refs, list):
        return None
    order_refs = [row for row in refs if isinstance(row, dict) and row.get("doctype") == "Sales Order"]
    if (
        mapping.get("source_entity_type") != "order"
        or mapping.get("erp_doctype") != "Sales Order"
        or context.get("native_action") != "modify_pending_order_address"
        or context.get("native_ids") != {"order_id": source_id}
        or evidence.get("event") != "on_update_after_submit"
        or evidence.get("document", {}).get("docstatus") != 1
        or source_order.get("order_id") != source_id
        or source_order.get("status") not in _PENDING_STATES
        or order.get("order_id") != source_id
        or order.get("status") not in _PENDING_STATES
        or order.get("user_id") != source_order.get("user_id")
        or order.get("items") != source_order.get("items")
        or order.get("payment_history") != source_order.get("payment_history")
        or order.get("fulfillments") != source_order.get("fulfillments")
        or order.get("address") == source_order.get("address")
        or len(order_refs) != 1
        or order_refs[0].get("name") != mapping.get("erp_name")
        or order_refs[0].get("docstatus") != 1
        or not isinstance(order_refs[0].get("modified"), str)
        or not order_refs[0]["modified"]
        or len(refs) != 1
    ):
        return None
    address = order.get("address")
    if not isinstance(address, dict):
        return None
    keys = ("address1", "address2", "city", "state", "country", "zip")
    if any(not isinstance(address.get(key), str) for key in keys):
        return None
    country = "USA" if address["country"] == "United States" else address["country"]
    arguments = {key: address[key] for key in keys if key != "country"}
    arguments.update({"country": country, "order_id": source_id})
    return arguments, {
        "kind": "tau_retail_current_erp_projection",
        "source_order": source_order,
        "order": order,
        "native_document_refs": refs,
    }


def _pending_order_address_action_projection(action: TranslatedAction) -> dict[str, Any] | None:
    projections = [row for row in action.supporting_evidence
                   if row.get("kind") == "tau_retail_current_erp_projection"]
    if len(projections) != 1:
        return None
    projection = projections[0]
    source_order, order = projection.get("source_order"), projection.get("order")
    if not isinstance(source_order, dict) or not isinstance(order, dict):
        return None
    if (
        source_order.get("order_id") != action.arguments.get("order_id")
        or source_order.get("status") not in _PENDING_STATES
        or order.get("order_id") != action.arguments.get("order_id")
        or order.get("status") not in _PENDING_STATES
        or order.get("address") != {
            "address1": action.arguments.get("address1"),
            "address2": action.arguments.get("address2"),
            "city": action.arguments.get("city"),
            "state": action.arguments.get("state"),
            "country": action.arguments.get("country"),
            "zip": action.arguments.get("zip"),
        }
    ):
        return None
    return projection


def _pending_order_change_projection(
    mapping: dict[str, Any], evidence: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Prove a pending-order item/payment change from posted ERP state."""
    context = mapping.get("native_context", {})
    action = context.get("native_action")
    source_id = mapping.get("source_record_id")
    observation = evidence.get("native_observation")
    order = observation.get("order") if isinstance(observation, dict) else None
    user = observation.get("user") if isinstance(observation, dict) else None
    source_order = context.get("source_snapshot")
    if not all(isinstance(value, dict) for value in (observation, order, user, source_order)):
        return None
    if (
        mapping.get("erp_doctype") != "Sales Order"
        or mapping.get("source_entity_type") != "order"
        or context.get("native_ids") != {"order_id": source_id}
        or evidence.get("event") != "on_update_after_submit"
        or evidence.get("erp_name") != mapping.get("erp_name")
        or evidence.get("document", {}).get("docstatus") != 1
        or source_order.get("order_id") != source_id
        or source_order.get("status") != "pending"
        or order.get("order_id") != source_id
        or order.get("user_id") != source_order.get("user_id")
        or order.get("address") != source_order.get("address")
        or order.get("fulfillments") != source_order.get("fulfillments")
        or user.get("user_id") != source_order.get("user_id")
        or not isinstance(user.get("payment_methods"), dict)
        or not isinstance(source_order.get("payment_history"), list)
        or not isinstance(order.get("payment_history"), list)
    ):
        return None
    refs = _order_payment_evidence_refs(mapping, evidence, order)
    if refs is None:
        return None

    old_history = source_order["payment_history"]
    new_history = order["payment_history"]
    if new_history[:len(old_history)] != old_history:
        return None
    appended = new_history[len(old_history):]

    if action == "modify_pending_order_items":
        source_items, current_items = source_order.get("items"), order.get("items")
        if (
            order.get("status") != "pending (item modified)"
            or not isinstance(source_items, list)
            or not isinstance(current_items, list)
            or not source_items
            or len(source_items) != len(current_items)
        ):
            return None
        item_ids, new_item_ids = [], []
        price_difference = 0.0
        for old_item, new_item in zip(source_items, current_items):
            if not isinstance(old_item, dict) or not isinstance(new_item, dict):
                return None
            old_id, new_id = old_item.get("item_id"), new_item.get("item_id")
            if (
                not isinstance(old_id, str) or not old_id
                or not isinstance(new_id, str) or not new_id
                or old_item.get("product_id") != new_item.get("product_id")
            ):
                return None
            if old_id == new_id:
                if old_item.get("price") != new_item.get("price"):
                    return None
                continue
            try:
                old_price = float(old_item["price"])
                new_price = float(new_item["price"])
            except (KeyError, TypeError, ValueError):
                return None
            if not (math.isfinite(old_price) and math.isfinite(new_price)):
                return None
            item_ids.append(old_id)
            new_item_ids.append(new_id)
            price_difference += new_price - old_price
        if not item_ids or len(appended) != 1:
            return None
        price_difference = round(price_difference, 2)
        transaction_type = "payment" if price_difference > 0 else "refund"
        transaction = appended[0]
        method_id = transaction.get("payment_method_id") if isinstance(transaction, dict) else None
        amount = _decimal_or_none(transaction.get("amount")) if isinstance(transaction, dict) else None
        if (
            not isinstance(method_id, str) or not method_id
            or method_id not in user["payment_methods"]
            or transaction.get("transaction_type") != transaction_type
            or amount != Decimal(str(abs(price_difference)))
        ):
            return None
        arguments = {
            "order_id": source_id, "item_ids": item_ids,
            "new_item_ids": new_item_ids, "payment_method_id": method_id,
        }
    elif action == "modify_pending_order_payment":
        if (
            order.get("status") != "pending"
            or order.get("items") != source_order.get("items")
            or len(old_history) != 1
            or not isinstance(old_history[0], dict)
            or old_history[0].get("transaction_type") != "payment"
            or len(appended) != 2
        ):
            return None
        original = old_history[0]
        new_payment, old_refund = appended
        if not isinstance(new_payment, dict) or not isinstance(old_refund, dict):
            return None
        new_method = new_payment.get("payment_method_id") if isinstance(new_payment, dict) else None
        old_method = original.get("payment_method_id")
        original_amount = _decimal_or_none(original.get("amount"))
        payment_amount = _decimal_or_none(new_payment.get("amount"))
        refund_amount = _decimal_or_none(old_refund.get("amount"))
        if (
            new_payment.get("transaction_type") != "payment"
            or old_refund.get("transaction_type") != "refund"
            or not isinstance(new_method, str) or not new_method
            or new_method == old_method
            or new_method not in user["payment_methods"]
            or old_method not in user["payment_methods"]
            or original_amount is None or payment_amount != original_amount
            or refund_amount != original_amount
            or old_refund.get("payment_method_id") != old_method
        ):
            return None
        arguments = {"order_id": source_id, "payment_method_id": new_method}
    else:
        return None

    return arguments, {
        "kind": "tau_retail_current_erp_projection",
        "source_order": source_order,
        "order": order,
        "user": user,
        "native_document_refs": refs,
    }


def _pending_order_change_action_projection(
    action: TranslatedAction,
) -> dict[str, Any] | None:
    projections = [
        row for row in action.supporting_evidence
        if row.get("kind") == "tau_retail_current_erp_projection"
    ]
    if len(projections) != 1:
        return None
    projection = projections[0]
    order, user, source_order = (
        projection.get("order"), projection.get("user"), projection.get("source_order")
    )
    if not all(isinstance(value, dict) for value in (order, user, source_order)):
        return None
    order_id = action.arguments.get("order_id")
    if (
        source_order.get("order_id") != order_id
        or order.get("order_id") != order_id
        or source_order.get("user_id") != user.get("user_id")
        or order.get("user_id") != user.get("user_id")
        or not isinstance(projection.get("native_document_refs"), list)
    ):
        return None
    if action.native_action == "modify_pending_order_items":
        if (
            source_order.get("status") != "pending"
            or order.get("status") != "pending (item modified)"
            or not isinstance(action.arguments.get("item_ids"), list)
            or not isinstance(action.arguments.get("new_item_ids"), list)
            or len(action.arguments["item_ids"]) != len(action.arguments["new_item_ids"])
            or not isinstance(action.arguments.get("payment_method_id"), str)
        ):
            return None
    elif action.native_action == "modify_pending_order_payment":
        if (
            source_order.get("status") != "pending"
            or order.get("status") != "pending"
            or not isinstance(action.arguments.get("payment_method_id"), str)
        ):
            return None
    else:
        return None
    return projection


def _pending_order_change_reconciles(
    action: TranslatedAction, before: dict[str, Any], after: dict[str, Any]
) -> tuple[bool, str]:
    projection = _pending_order_change_action_projection(action)
    if projection is None:
        return False, "posted native order or payment evidence is incomplete"
    order_id = action.arguments["order_id"]
    user_id = projection["user"]["user_id"]
    before_order, after_order = before.get("orders", {}).get(order_id), after.get("orders", {}).get(order_id)
    before_user, after_user = before.get("users", {}).get(user_id), after.get("users", {}).get(user_id)
    if not all(isinstance(value, dict) for value in (before_order, after_order, before_user, after_user)):
        return False, "τ pending-order update is missing its order or customer"
    source_order, projected_order = projection["source_order"], projection["order"]
    projected_methods = projection["user"].get("payment_methods")
    if (
        before_order.get("status") != source_order.get("status")
        or before_order.get("items") != source_order.get("items")
        or before_order.get("payment_history") != source_order.get("payment_history")
        or before_order.get("address") != source_order.get("address")
    ):
        return False, "τ starting checkpoint differs from the native source order facts"
    if after_order != projected_order:
        return False, "upstream τ order result differs from the current ERP-observed order"
    if (
        not isinstance(projected_methods, dict)
        or after_user.get("payment_methods") != projected_methods
    ):
        return False, "upstream τ gift-card and payment-method effects differ from posted ERP state"
    if action.native_action == "modify_pending_order_payment" and before_order.get("items") != after_order.get("items"):
        return False, "payment-method change unexpectedly altered native order items"
    return True, "upstream τ order and payment effects match posted ERP state"


def _pending_order_address_reconciles(
    action: TranslatedAction, before: dict[str, Any], after: dict[str, Any]
) -> tuple[bool, str]:
    projection = _pending_order_address_action_projection(action)
    if projection is None:
        return False, "native pending-order address evidence is incomplete"
    order_id = action.arguments["order_id"]
    before_order, after_order = before.get("orders", {}).get(order_id), after.get("orders", {}).get(order_id)
    if not isinstance(before_order, dict) or not isinstance(after_order, dict):
        return False, "τ address update is missing its order"
    source_order, projected_order = projection["source_order"], projection["order"]
    if (
        before_order.get("status") != source_order.get("status")
        or before_order.get("items") != source_order.get("items")
        or before_order.get("payment_history") != source_order.get("payment_history")
        or before_order.get("fulfillments") != source_order.get("fulfillments")
    ):
        return False, "τ starting checkpoint differs from the native source order facts"
    if after_order != projected_order:
        return False, "upstream τ address result differs from current ERP-observed order"
    return True, "upstream τ address update matches the linked ERP Address"


def translate(mapping: Any, evidence: Any) -> dict[str, Any]:
    """Translate one source mapping and native company evidence pair.

    Supported native_context actions use exact native IDs and exact ERP field
    paths. Other business changes remain visible through native observation,
    but receive no fabricated τ action.
    """
    route, source = _plain(mapping), _plain(evidence)
    identity_result = _identity(route, source)
    if identity_result is not None:
        return identity_result
    context = route["native_context"]
    native_action = context.get("native_action")
    if native_action not in {
        "cancel_pending_order",
        "modify_pending_order_address",
        "modify_user_address",
        "modify_pending_order_items",
        "return_delivered_order_items",
        "exchange_delivered_order_items",
        "modify_pending_order_payment",
    }:
        return _result(
            "unmapped", "native ERP transition has no confirmed τ retail action mapping"
        )
    events = context.get("events")
    if not isinstance(events, list) or source.get("event") not in events:
        return _result(
            "unmapped", "native evidence event is not enabled by this action mapping"
        )
    if native_action in (
        "return_delivered_order_items",
        "exchange_delivered_order_items",
    ):
        if source.get("event") not in ("on_update", "on_submit"):
            return _result("unmapped", "native Issue request event is not enabled for a τ return or exchange")
        translated = _service_request_arguments(route, source)
        if translated is None:
            return _result("inconclusive", "native Issue request and current ERP order projection do not establish the exact τ request fields")
        arguments, projection = translated
        with_projection = dict(source)
        with_projection["supporting_evidence"] = [projection]
        return _translated_action(
            route, with_projection, native_action, arguments,
            [f"tau2-bench:{UPSTREAM_COMMIT}", "ERPNext customer service Issue request"],
        )
    if native_action in {"modify_pending_order_items", "modify_pending_order_payment"}:
        if (
            route.get("source_entity_type") != "order"
            or route.get("erp_doctype") != "Sales Order"
            or source.get("event") != "on_update_after_submit"
        ):
            return _result("unmapped", "τ pending-order updates require Sales Order on_update_after_submit evidence")
        translated = _pending_order_change_projection(route, source)
        if translated is None:
            return _result(
                "inconclusive",
                "native order, item identities, and posted payment history do not prove the exact τ update",
            )
        arguments, projection = translated
        with_projection = dict(source)
        with_projection["supporting_evidence"] = [projection]
        return _translated_action(
            route, with_projection, native_action, arguments,
            [f"tau2-bench:{UPSTREAM_COMMIT}", "ERPNext Sales Order and posted Payment Entry state"],
        )

    source_id = route.get("source_record_id")
    if native_action == "cancel_pending_order":
        if (
            route.get("source_entity_type") != "order"
            or route.get("erp_doctype") != "Sales Order"
            or source.get("event") != "on_cancel"
        ):
            return _result(
                "unmapped",
                "τ pending-order cancellation requires Sales Order on_cancel evidence",
            )
        native_ids = context.get("native_ids")
        if not isinstance(native_ids, dict) or native_ids != {"order_id": source_id}:
            return _result(
                "inconclusive", "cancellation must bind the exact mapped τ order ID"
            )
        fields = _mapped_fields(
            context,
            source,
            {
                "before_state",
                "docstatus",
                "current_state",
                "reason",
            },
        )
        if fields is None:
            return _result(
                "inconclusive",
                "cancellation requires exact prior/current workflow and reason fields",
            )
        # τ's cancel_pending_order accepts exactly `pending`; it rejects the
        # distinct `pending (item modified)` state.
        if (
            fields["before_state"] != "Pending"
            or fields["docstatus"] != 2
            or fields["current_state"] != "Cancelled"
            or fields["reason"] not in _CANCEL_REASONS
        ):
            return _result(
                "inconclusive",
                "native cancellation does not prove τ pending-order cancellation semantics",
            )
        refund_proven, refund_reason, supporting_evidence = _cancel_refund_proof(
            context, source
        )
        if not refund_proven:
            return _result("inconclusive", refund_reason)
        translated_evidence = dict(source)
        translated_evidence["supporting_evidence"] = supporting_evidence
        return _translated_action(
            route,
            translated_evidence,
            native_action,
            {"order_id": source_id, "reason": fields["reason"]},
            [f"tau2-bench:{UPSTREAM_COMMIT}", "ERPNext Sales Order on_cancel"],
        )

    if native_action == "modify_pending_order_address":
        if (
            route.get("source_entity_type") != "order"
            or route.get("erp_doctype") != "Sales Order"
            or source.get("event") != "on_update_after_submit"
        ):
            return _result(
                "unmapped",
                "pending-order address change requires Sales Order on_update_after_submit evidence",
            )
        projection = _pending_order_address_projection(route, source)
        if projection is not None:
            arguments, native_projection = projection
            with_projection = dict(source)
            with_projection["supporting_evidence"] = [native_projection]
            return _translated_action(
                route, with_projection, native_action, arguments,
                [f"tau2-bench:{UPSTREAM_COMMIT}", "ERPNext Sales Order linked Address update"],
            )
        native_ids = context.get("native_ids")
        if not isinstance(native_ids, dict) or native_ids != {"order_id": source_id}:
            return _result(
                "inconclusive", "address change must bind the exact mapped τ order ID"
            )
        fields = _mapped_fields(
            context,
            source,
            {
                "address1",
                "address2",
                "city",
                "state",
                "country",
                "zip",
                "docstatus",
                "before_state",
                "current_state",
            },
        )
        if fields is None:
            return _result(
                "inconclusive",
                "address change requires exact prior/current order and address fields",
            )
        if (
            fields["docstatus"] != 1
            or fields["before_state"] not in _PENDING_STATES
            or fields["current_state"] not in _PENDING_STATES
        ):
            return _result(
                "inconclusive",
                "native order was not pending before and after its address update",
            )
        country = "USA" if fields["country"] == "United States" else fields["country"]
        args = {
            key: fields[key] for key in ("address1", "address2", "city", "state", "zip")
        }
        args.update({"country": country, "order_id": source_id})
        if any(
            not isinstance(args[key], str)
            for key in ("address1", "address2", "city", "state", "country", "zip")
        ):
            return _result(
                "inconclusive",
                "native address values do not map to τ scalar address fields",
            )
        return _translated_action(
            route,
            source,
            native_action,
            args,
            [f"tau2-bench:{UPSTREAM_COMMIT}", "ERPNext Sales Order address update"],
        )

    if native_action == "modify_user_address":
        if (
            route.get("source_entity_type") != "user"
            or route.get("erp_doctype") != "Address"
            or source.get("event") != "on_update"
        ):
            return _result(
                "unmapped",
                "user address change requires linked Address on_update evidence",
            )
        native_ids = context.get("native_ids")
        if (
            not isinstance(native_ids, dict)
            or set(native_ids) != {"user_id", "customer_name"}
            or native_ids.get("user_id") != source_id
            or not isinstance(native_ids.get("customer_name"), str)
        ):
            return _result(
                "inconclusive",
                "user address mapping must bind the exact customer and τ user IDs",
            )
        links = source["document"].get("links")
        if not isinstance(links, list) or not any(
            link.get("link_doctype") == "Customer"
            and link.get("link_name") == native_ids["customer_name"]
            for link in links
            if isinstance(link, dict)
        ):
            return _result(
                "inconclusive",
                "updated Address is not linked to the mapped native Customer",
            )
        fields = _mapped_fields(
            context, source, {"address1", "address2", "city", "state", "country", "zip"}
        )
        if fields is None:
            return _result(
                "inconclusive",
                "user address mapping requires exact native Address fields",
            )
        country = "USA" if fields["country"] == "United States" else fields["country"]
        args = {
            key: fields[key] for key in ("address1", "address2", "city", "state", "zip")
        }
        args.update({"country": country, "user_id": source_id})
        if any(
            not isinstance(args[key], str)
            for key in ("address1", "address2", "city", "state", "country", "zip")
        ):
            return _result(
                "inconclusive",
                "native address values do not map to τ scalar address fields",
            )
        return _translated_action(
            route,
            source,
            native_action,
            args,
            [f"tau2-bench:{UPSTREAM_COMMIT}", "ERPNext linked Customer Address update"],
        )

    return _result(
        "unmapped", "native ERP transition has no supported τ retail action mapping"
    )


_NATIVE_ACTIONS = frozenset(
    {
        "cancel_pending_order",
        "modify_pending_order_address",
        "modify_user_address",
        "modify_pending_order_items",
        "modify_pending_order_payment",
        "return_delivered_order_items",
        "exchange_delivered_order_items",
    }
)


def _database_state(environment: Any) -> dict[str, Any]:
    db = getattr(getattr(environment, "tools", None), "db", None)
    if db is None or not hasattr(db, "model_dump"):
        raise TypeError("Expected the pinned τ Retail environment with a RetailDB")
    return db.model_dump(mode="json", by_alias=True)


def _retail_state_ref(state: dict[str, Any]) -> str:
    return "tau-retail-db:" + hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _rejected_action(action: TranslatedAction, before: dict[str, Any], reason: str) -> NativeActionReceipt:
    return NativeActionReceipt(
        action_id=action.action_id,
        accepted=False,
        reconciled=False,
        native_state_ref=_retail_state_ref(before),
        outcome={"status": "inconclusive", "reason": reason},
    )


def _cancel_projection(action: TranslatedAction) -> dict[str, Any] | None:
    projections = [
        item for item in action.supporting_evidence
        if item.get("kind") == "tau_retail_current_erp_projection"
    ]
    if len(projections) != 1:
        return None
    projection = projections[0]
    order, user, source_order = (
        projection.get("order"), projection.get("user"), projection.get("source_order")
    )
    if not all(isinstance(value, dict) for value in (order, user, source_order)):
        return None
    refs = projection.get("native_document_refs")
    if not isinstance(refs, list):
        return None
    order_refs = [ref for ref in refs if isinstance(ref, dict) and ref.get("doctype") == "Sales Order"]
    payment_refs = [ref for ref in refs if isinstance(ref, dict) and ref.get("doctype") == "Payment Entry"]
    if (
        len(order_refs) != 1 or order_refs[0].get("docstatus") != 2
        or not order_refs[0].get("modified")
        or len(payment_refs) != len(order.get("payment_history", []))
        or any(ref.get("docstatus") != 1 or not ref.get("modified") for ref in payment_refs)
    ):
        return None
    order_id = action.arguments.get("order_id")
    if (
        order.get("order_id") != order_id
        or source_order.get("order_id") != order_id
        or source_order.get("user_id") != user.get("user_id")
    ):
        return None
    return projection


def _cancellation_reconciles(
    action: TranslatedAction,
    before: dict[str, Any],
    after: dict[str, Any],
) -> tuple[bool, str]:
    projected = _cancel_projection(action)
    if projected is None:
        return False, "current ERP projection or supporting native references are incomplete"
    before_order = before.get("orders", {}).get(action.arguments["order_id"])
    before_user = before.get("users", {}).get(projected["user"].get("user_id"))
    source_order = projected["source_order"]
    projected_order, projected_user = projected["order"], projected["user"]
    order_id = action.arguments["order_id"]
    after_order = after.get("orders", {}).get(order_id)
    after_user = after.get("users", {}).get(before_user.get("user_id"))
    if not isinstance(after_order, dict) or not isinstance(after_user, dict):
        return False, "native cancellation result is missing the order or customer payment methods"
    if not isinstance(before_order, dict) or not isinstance(before_user, dict):
        return False, "τ starting checkpoint is missing the mapped order or customer"
    if before_order.get("status") != source_order.get("status") or before_order.get("payment_history") != source_order.get("payment_history"):
        return False, "τ starting checkpoint differs from the source order facts"
    if after_order != projected_order:
        return False, "native cancellation order state differs from the current ERP projection"
    if after_user.get("payment_methods") != projected_user.get("payment_methods"):
        return False, "native cancellation customer payment state differs from the current ERP projection"
    if after_order.get("status") != "cancelled" or after_order.get("cancel_reason") != action.arguments.get("reason"):
        return False, "native cancellation result does not match requested order status and reason"
    old_history = before_order.get("payment_history")
    new_history = after_order.get("payment_history")
    if not isinstance(old_history, list) or not isinstance(new_history, list):
        return False, "native cancellation result has no payment history"
    refunds = new_history[len(old_history):]
    if new_history[:len(old_history)] != old_history or len(refunds) != len(old_history):
        return False, "native cancellation result does not append one refund per original transaction"
    expected_refunds = []
    for original in old_history:
        if not isinstance(original, dict):
            return False, "checkpoint payment history has an invalid transaction"
        expected_refunds.append({
            "transaction_type": "refund",
            "amount": original.get("amount"),
            "payment_method_id": original.get("payment_method_id"),
        })
    if refunds != expected_refunds:
        return False, "native cancellation refund rows differ from the actual original transactions"
    expected_methods = copy.deepcopy(before_user.get("payment_methods"))
    actual_methods = after_user.get("payment_methods")
    if not isinstance(expected_methods, dict) or not isinstance(actual_methods, dict):
        return False, "native cancellation result has no customer payment-method state"
    for refund in refunds:
        method = expected_methods.get(refund["payment_method_id"])
        if not isinstance(method, dict):
            return False, "native refund references an unknown customer payment method"
        if method.get("source") == "gift_card":
            try:
                method["balance"] = round(float(method["balance"]) + float(refund["amount"]), 2)
            except (KeyError, TypeError, ValueError):
                return False, "customer gift-card balance is not verifiable from the ERP projection"
    if actual_methods != expected_methods:
        return False, "native cancellation result does not reconcile customer gift-card refund balances"
    return True, "native order refunds and customer gift-card balances reconcile to the ERP projection"


def _service_request_projection(action: TranslatedAction) -> dict[str, Any] | None:
    projections = [
        item for item in action.supporting_evidence
        if item.get("kind") == "tau_retail_current_erp_projection"
    ]
    if len(projections) != 1:
        return None
    projection = projections[0]
    order, user, source_order, issue = (
        projection.get("order"), projection.get("user"),
        projection.get("source_order"), projection.get("service_issue"),
    )
    if not all(isinstance(value, dict) for value in (order, user, source_order, issue)):
        return None
    expected_status = "return requested" if action.native_action == "return_delivered_order_items" else "exchange requested"
    order_id = action.arguments.get("order_id")
    if (
        source_order.get("order_id") != order_id or source_order.get("status") != "delivered"
        or order.get("order_id") != order_id or order.get("status") != expected_status
        or user.get("user_id") != source_order.get("user_id")
        or issue.get("status") != "Open"
    ):
        return None
    refs = projection.get("native_document_refs")
    if not isinstance(refs, list):
        return None
    sales_order = [ref for ref in refs if isinstance(ref, dict) and ref.get("doctype") == "Sales Order"]
    issues = [ref for ref in refs if isinstance(ref, dict) and ref.get("doctype") == "Issue"]
    if (
        len(sales_order) != 1 or sales_order[0].get("docstatus") != 1
        or not sales_order[0].get("modified")
        or len(issues) != 1 or issues[0].get("name") != issue.get("name")
        or issues[0].get("docstatus") not in (0, 1) or not issues[0].get("modified")
    ):
        return None
    return projection


def _service_request_reconciles(
    action: TranslatedAction, before: dict[str, Any], after: dict[str, Any]
) -> tuple[bool, str]:
    projection = _service_request_projection(action)
    if projection is None:
        return False, "native ERP return or exchange request projection is incomplete"
    order_id = action.arguments["order_id"]
    user_id = projection["user"]["user_id"]
    before_order = before.get("orders", {}).get(order_id)
    after_order = after.get("orders", {}).get(order_id)
    before_user = before.get("users", {}).get(user_id)
    after_user = after.get("users", {}).get(user_id)
    if not all(isinstance(value, dict) for value in (before_order, after_order, before_user, after_user)):
        return False, "τ request result is missing the order or customer"
    source_order, projected_order = projection["source_order"], projection["order"]
    projected_methods = projection["user"].get("payment_methods")
    if (
        before_order.get("status") != source_order.get("status")
        or before_order.get("items") != source_order.get("items")
        or before_order.get("payment_history") != source_order.get("payment_history")
    ):
        return False, "τ starting checkpoint differs from the delivered source order facts"
    if (
        after_order.get("status") != projected_order.get("status")
        or after_order.get("items") != source_order.get("items")
        or after_order.get("payment_history") != source_order.get("payment_history")
        or after_user.get("payment_methods") != before_user.get("payment_methods")
    ):
        return False, "request-only τ return or exchange changed financial state"
    if action.native_action == "return_delivered_order_items":
        matches = (
            after_order.get("return_items") == sorted(action.arguments.get("item_ids", []))
            and after_order.get("return_payment_method_id") == action.arguments.get("payment_method_id")
        )
    else:
        matches = (
            after_order.get("exchange_items") == sorted(action.arguments.get("item_ids", []))
            and after_order.get("exchange_new_items") == sorted(action.arguments.get("new_item_ids", []))
            and after_order.get("exchange_payment_method_id") == action.arguments.get("payment_method_id")
            and after_order.get("exchange_price_difference") == projected_order.get("exchange_price_difference")
        )
    if not matches:
        return False, "upstream τ request fields differ from the actual ERP Issue request"
    return True, "native τ request state matches the ERP Issue without a financial effect"


def apply_native(environment: Any, action: Any) -> NativeActionReceipt:
    """Apply one confirmed action through τ's pinned native RetailTools.

    The caller owns checkpoint transactions and receipt persistence. This
    function mutates only the supplied environment and verifies the resulting
    native DB state before returning an accepted receipt.
    """
    translated = TranslatedAction.model_validate(_plain(action))
    if (
        translated.benchmark != "tau_retail"
        or translated.native_action not in _NATIVE_ACTIONS
    ):
        raise ValueError("Unsupported native τ retail feedback action")
    args = translated.arguments
    before = copy.deepcopy(_database_state(environment))
    if translated.native_action == "cancel_pending_order" and _cancel_projection(translated) is None:
        return _rejected_action(translated, before, "current ERP projection or supporting native references are incomplete")
    if translated.native_action in {"return_delivered_order_items", "exchange_delivered_order_items"} and _service_request_projection(translated) is None:
        return _rejected_action(translated, before, "native ERP return or exchange request projection is incomplete")
    if translated.native_action in {"modify_pending_order_items", "modify_pending_order_payment"} and _pending_order_change_action_projection(translated) is None:
        return _rejected_action(translated, before, "posted native pending-order update evidence is incomplete")
    if translated.native_action == "modify_pending_order_address" and _pending_order_address_action_projection(translated) is None:
        return _rejected_action(translated, before, "native pending-order address projection is incomplete")
    tool_result = environment.make_tool_call(
        translated.native_action,
        requestor="assistant",
        **args,
    )
    after = _database_state(environment)
    if before == after:
        return _rejected_action(translated, before, "τ native tool produced no state change")

    orders = after.get("orders", {})
    users = after.get("users", {})
    outcome: dict[str, Any]
    if translated.native_action == "cancel_pending_order":
        reconciled, reason = _cancellation_reconciles(translated, before, after)
        if not reconciled:
            return _rejected_action(translated, before, reason)
        order = orders.get(args["order_id"])
        if (
            not isinstance(order, dict)
            or order.get("status") != "cancelled"
            or order.get("cancel_reason") != args["reason"]
        ):
            raise RuntimeError(
                "τ cancellation tool result did not reconcile to the requested native order state"
            )
        outcome = {
            "order_id": args["order_id"],
            "status": order["status"],
            "cancel_reason": order["cancel_reason"],
        }
    elif translated.native_action in {"return_delivered_order_items", "exchange_delivered_order_items"}:
        reconciled, reason = _service_request_reconciles(translated, before, after)
        if not reconciled:
            return _rejected_action(translated, before, reason)
        order = orders.get(args["order_id"])
        outcome = {
            "order_id": args["order_id"],
            "status": order["status"],
            "return_items": order.get("return_items"),
            "exchange_items": order.get("exchange_items"),
            "exchange_new_items": order.get("exchange_new_items"),
        }
    elif translated.native_action in {"modify_pending_order_items", "modify_pending_order_payment"}:
        reconciled, reason = _pending_order_change_reconciles(translated, before, after)
        if not reconciled:
            return _rejected_action(translated, before, reason)
        order = orders.get(args["order_id"])
        if not isinstance(order, dict):
            return _rejected_action(translated, before, "τ update result is missing the order")
        outcome = {
            "order_id": args["order_id"], "status": order["status"],
            "items": order.get("items"), "payment_history": order.get("payment_history"),
        }
    elif translated.native_action == "modify_pending_order_address":
        reconciled, reason = _pending_order_address_reconciles(translated, before, after)
        if not reconciled:
            return _rejected_action(translated, before, reason)
        order = orders.get(args["order_id"])
        expected_address = {
            key: args[key]
            for key in ("address1", "address2", "city", "state", "country", "zip")
        }
        if not isinstance(order, dict) or order.get("address") != expected_address:
            raise RuntimeError(
                "τ address tool result did not reconcile to the requested native order address"
            )
        outcome = {"order_id": args["order_id"], "address": expected_address}
    else:
        user = users.get(args["user_id"])
        expected_address = {
            key: args[key]
            for key in ("address1", "address2", "city", "state", "country", "zip")
        }
        if not isinstance(user, dict) or user.get("address") != expected_address:
            raise RuntimeError(
                "τ address tool result did not reconcile to the requested native user address"
            )
        outcome = {"user_id": args["user_id"], "address": expected_address}

    # Keep this reference content-free: the full state may contain customer
    # contact and payment-method data.
    state_ref = _retail_state_ref(after)
    return NativeActionReceipt(
        action_id=translated.action_id,
        accepted=True,
        reconciled=True,
        native_state_ref=state_ref,
        outcome={
            "native_action": translated.native_action,
            "result_type": type(tool_result).__name__,
            **outcome,
        },
    )


def initialize_native_state(
    store: Any, task_id: str, experiment_id: str, observation: Any
) -> dict[str, Any]:
    """Persist the ERP observer's exact projection as an experiment task checkpoint."""
    from servers.simulation.tau2_retail import load_observed_db, task_manifest

    manifest = task_manifest(task_id)
    if manifest.get("task_id") != task_id:
        raise ValueError("τ retail task manifest identity mismatch")
    database = load_observed_db(observation)
    database_json = json.dumps(
        database.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    projection_hash = hashlib.sha256(database_json.encode("utf-8")).hexdigest()
    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT upstream_commit,initial_projection_hash FROM tau_retail_feedback_states WHERE experiment_id=? AND task_id=?",
            (experiment_id, task_id),
        ).fetchone()
        if prior is not None:
            if prior["upstream_commit"] != UPSTREAM_COMMIT:
                connection.rollback()
                raise ValueError(
                    "τ retail native state uses a different upstream source revision"
                )
            connection.commit()
            return {
                "task_id": task_id,
                "experiment_id": experiment_id,
                "status": "already_initialized",
                "projection_matches_initial": prior["initial_projection_hash"]
                == projection_hash,
            }
        connection.execute(
            "INSERT INTO tau_retail_feedback_states "
            "(experiment_id,task_id,upstream_commit,initial_projection_hash,database_json,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                experiment_id,
                task_id,
                UPSTREAM_COMMIT,
                projection_hash,
                database_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {"task_id": task_id, "experiment_id": experiment_id, "status": "initialized"}


def apply_persisted_native(
    store: Any, task_id: str, experiment_id: str, action: Any
) -> NativeActionReceipt:
    """Apply one tool call and atomically checkpoint its state and receipt."""
    from servers.simulation.tau2_retail import _ensure_upstream_data_root, task_manifest

    _ensure_upstream_data_root()
    from tau2.domains.retail.data_model import RetailDB
    from tau2.domains.retail.environment import get_environment

    translated = TranslatedAction.model_validate(_plain(action))
    if translated.benchmark != "tau_retail":
        raise ValueError("native τ retail action benchmark mismatch")
    action_id = translated.action_id
    request_json = json.dumps(
        {
            "task_id": task_id,
            "experiment_id": experiment_id,
            "action": translated.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    source_records = task_manifest(task_id).get("original_db", {})
    entity = "users" if translated.native_action == "modify_user_address" else "orders"
    source_id = translated.arguments.get("user_id" if entity == "users" else "order_id")
    if not isinstance(source_id, str) or source_id not in source_records.get(
        entity, {}
    ):
        raise ValueError(
            "τ retail feedback action target is outside the task-linked source records"
        )

    connection = store._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT request_json,receipt_json FROM tau_retail_feedback_receipts "
            "WHERE experiment_id=? AND task_id=? AND action_id=?",
            (experiment_id, task_id, action_id),
        ).fetchone()
        if prior is not None:
            if prior["request_json"] != request_json:
                connection.rollback()
                raise ValueError(
                    "τ retail action ID was reused with different evidence"
                )
            connection.commit()
            return NativeActionReceipt.model_validate_json(prior["receipt_json"])
        state = connection.execute(
            "SELECT upstream_commit,database_json FROM tau_retail_feedback_states WHERE experiment_id=? AND task_id=?",
            (experiment_id, task_id),
        ).fetchone()
        if state is None or state["upstream_commit"] != UPSTREAM_COMMIT:
            connection.rollback()
            raise LookupError(
                "τ retail native state has not been initialized from ERP evidence"
            )
        db = RetailDB.model_validate_json(state["database_json"])
        environment = get_environment(db=db)
        receipt = apply_native(environment, translated)
        receipt_json = receipt.model_dump_json()
        if receipt.accepted and receipt.reconciled:
            updated = environment.tools.db.model_dump(mode="json", by_alias=True)
            database_json = json.dumps(
                updated, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            connection.execute(
                "UPDATE tau_retail_feedback_states SET database_json=?,updated_at=? WHERE experiment_id=? AND task_id=?",
                (
                    database_json,
                    datetime.now(timezone.utc).isoformat(),
                    experiment_id,
                    task_id,
                ),
            )
        connection.execute(
            "INSERT INTO tau_retail_feedback_receipts VALUES(?,?,?,?,?)",
            (experiment_id, task_id, action_id, request_json, receipt_json),
        )
        connection.commit()
        return receipt
    except BaseException:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def reconcile_persisted_native(
    store: Any, task_id: str, experiment_id: str, action: Any
) -> NativeActionReceipt | None:
    translated = TranslatedAction.model_validate(_plain(action))
    request_json = json.dumps(
        {
            "task_id": task_id,
            "experiment_id": experiment_id,
            "action": translated.model_dump(mode="json"),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    connection = store._connect()
    try:
        row = connection.execute(
            "SELECT request_json,receipt_json FROM tau_retail_feedback_receipts "
            "WHERE experiment_id=? AND task_id=? AND action_id=?",
            (experiment_id, task_id, translated.action_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    if row["request_json"] != request_json:
        raise ValueError("τ retail action ID was reused with different evidence")
    return NativeActionReceipt.model_validate_json(row["receipt_json"])


class TauRetailFeedbackAdapter:
    """Translate and delegate company-feedback actions to the τ worker.

    The callbacks provide the common transactional checkpoint/receipt store
    and the original criteria evaluator; this adapter owns no benchmark state.
    """

    def __init__(
        self,
        apply_action: Any,
        reconcile_action: Any,
        evaluate_native: Any,
        enrich_evidence: Any | None = None,
    ):
        self._apply_action = apply_action
        self._reconcile_action = reconcile_action
        self._evaluate_native = evaluate_native
        self._enrich_evidence = enrich_evidence

    def translate(self, mapping: Any, evidence: Any) -> TranslationResult:
        if self._enrich_evidence is not None:
            evidence = self._enrich_evidence(_plain(mapping), _plain(evidence))
        return TranslationResult.model_validate(translate(mapping, evidence))

    def apply(self, action: Any) -> NativeActionReceipt:
        return NativeActionReceipt.model_validate(self._apply_action(_plain(action)))

    def reconcile(self, action: Any) -> NativeActionReceipt | None:
        result = self._reconcile_action(_plain(action))
        return None if result is None else NativeActionReceipt.model_validate(result)

    def evaluate(self, mappings: list[Any], evidence: list[Any]) -> dict[str, Any]:
        evidence_by_doc = {
            (_plain(item).get("erp_doctype"), _plain(item).get("erp_name")): item
            for item in evidence
        }
        translations = []
        settlement_outcomes = []
        for mapping in mappings:
            route = _plain(mapping)
            item = evidence_by_doc.get(
                (route.get("erp_doctype"), route.get("erp_name"))
            )
            if item is None:
                translations.append(
                    {
                        "mapping_id": route.get("mapping_id"),
                        "status": "inconclusive",
                        "reason": "no matching company evidence supplied",
                    }
                )
            else:
                raw_evidence = _plain(item)
                enriched = (
                    self._enrich_evidence(route, raw_evidence)
                    if self._enrich_evidence is not None
                    else raw_evidence
                )
                translations.append(
                    TranslationResult.model_validate(translate(route, enriched))
                    .model_dump(mode="json")
                )
                native_observation = enriched.get("native_observation")
                issue = (
                    native_observation.get("service_issue")
                    if isinstance(native_observation, dict)
                    else None
                )
                settlement = issue.get("native_settlement") if isinstance(issue, dict) else None
                if isinstance(settlement, dict):
                    settlement_outcomes.append({
                        "mapping_id": route.get("mapping_id"),
                        "source_record_id": route.get("source_record_id"),
                        "native_outcome": settlement,
                    })
        actions_by_id = {
            row["action"]["action_id"]: row["action"]
            for row in translations
            if row.get("action")
        }
        receipts = [self.reconcile(action) for action in actions_by_id.values()]
        native_evaluation = self._evaluate_native(mappings, evidence)
        return {
            "provider_id": "tau_retail",
            "upstream_commit": UPSTREAM_COMMIT,
            "projection_label": "company-adapted τ Retail projection; native ERP evidence is authoritative",
            "translations": translations,
            "native_action_receipts": [
                None if receipt is None else receipt.model_dump(mode="json")
                for receipt in receipts
            ],
            "native_evaluation": native_evaluation,
            "native_settlement_outcomes": settlement_outcomes,
            "coverage": [
                f"tau2-bench:{UPSTREAM_COMMIT}",
                "RetailTools native action API",
                "original τ evaluation criteria",
                "native_settlement_not_defined",
            ],
        }


__all__ = [
    "translate",
    "apply_native",
    "initialize_native_state",
    "apply_persisted_native",
    "reconcile_persisted_native",
    "TauRetailFeedbackAdapter",
]

"""Scripted S01 infrastructure reference through the production engine.

Call :func:`run_s01_reference` with ``app.state.engine`` from
``servers.simulation.runtime.build_app``. The runner uses the configured native
Mailu/SMTPDev and ERPNext adapters; it never fabricates inbound mail or native
ERP results. It is a deterministic reference actor, not a Viv performance run.
"""

from __future__ import annotations

import re
import json
import logging
import os
import time
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import requests

from servers.simulation.engine import EngineConflict, SimulationEngine
from servers.simulation.mail import SMTP4DevAPIError
from servers.simulation.models import (
    AdvanceClock, ERPAction, SendMessage, StartScenario, UpdateCase,
)


class ReferenceTrajectoryIncomplete(RuntimeError):
    """A native reference step or objective check did not complete."""


def _document_id(document: Any) -> str:
    if isinstance(document, str):
        return document
    if isinstance(document, dict):
        value = document.get("name") or document.get("id")
        if value:
            return str(value)
    raise ReferenceTrajectoryIncomplete("native run binding lacks a required document identity")


def _require_message(messages: list[dict[str, Any]], *, contains: str, from_actor: str,
                     engine: SimulationEngine, run_id: str) -> dict[str, Any]:
    actor = engine.store.get_actor(run_id, engine.store.get_run(run_id)["generation"], from_actor)
    address = str(actor.get("email", "")).casefold()
    found = next((message for message in messages
        if message.get("message_id")
        and address and address in str(message.get("from", "")).casefold()
        and contains.casefold() in (str(message.get("subject", "")) + "\n"
                                    + str(message.get("body", ""))).casefold()), None)
    if found is None:
        raise ReferenceTrajectoryIncomplete(
            f"native Viv mailbox did not contain the expected message from {from_actor}"
        )
    return found


def _advance_day(engine: SimulationEngine, run_id: str, initial: datetime, day: int,
                 key: str, *, expected_from: str, contains: str,
                 timeout_seconds: float = 180) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    target = initial + timedelta(days=day - 1)
    current = engine.store.get_run(run_id)
    current_time = datetime.fromisoformat(current["simulated_time"])
    # A reference retry may resume an already-advanced run. Never rewind its
    # clock; prior messages and actions are recovered through their stable IDs.
    result = (engine.advance(AdvanceClock(run_id=run_id, until=target), key)
              if target > current_time else {"simulated_time": current_time.isoformat(), "already_advanced": True})
    message = _wait_for_agent_message(engine, run_id, "viv", from_actor=expected_from,
                                      contains=contains, timeout_seconds=timeout_seconds)
    messages = engine.inbox(run_id, "viv")
    if message not in messages:
        messages.append(message)
    return result, messages


def _wait_for_agent_message(engine: SimulationEngine, run_id: str, actor_id: str, *,
                            from_actor: str, contains: str, reply_to_message_id: str | None = None,
                            timeout_seconds: float = 180) -> dict[str, Any]:
    """Wait on the runtime's native Viv notification condition, never by sleeping."""
    deadline = time.monotonic() + timeout_seconds
    last_transient_read_error: Exception | None = None
    waiter = getattr(engine, "wait_for_viv_activity", None)
    token_provider = getattr(engine, "viv_activity_version", None)
    if not callable(waiter) or not callable(token_provider):
        raise ReferenceTrajectoryIncomplete(
            "runtime does not expose its native bounded Viv mailbox notification waiter"
        )
    while True:
        token = token_provider()
        try:
            engine.pump(run_id)
            messages = engine.inbox(run_id, actor_id)
        except Exception as error:
            if not _is_transient_mailbox_read_error(error):
                raise
            last_transient_read_error = error
            remaining = deadline - time.monotonic()
            logging.getLogger(__name__).warning(
                "transient native mailbox read failed exception_type=%s operation=%s status=%s",
                type(error).__name__, getattr(error, "operation", "capture mailbox confirmation"),
                getattr(error, "status", None),
            )
            if remaining <= 0:
                raise ReferenceTrajectoryIncomplete(
                    f"native Viv mailbox read did not recover before waiting for {from_actor}"
                ) from error
            waiter(token, min(remaining, 5.0))
            continue
        actor = engine.store.get_actor(run_id, engine.store.get_run(run_id)["generation"], from_actor)
        address = str(actor.get("email", "")).casefold()
        found = next((message for message in messages
            if address and address in str(message.get("from", "")).casefold()
            and contains.casefold() in (str(message.get("subject", "")) + "\n"
                                        + str(message.get("body", ""))).casefold()
            and (reply_to_message_id is None or message.get("in_reply_to") == reply_to_message_id)), None)
        if found is not None:
            return found
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            incomplete = ReferenceTrajectoryIncomplete(
                f"native Viv mailbox did not receive the expected message from {from_actor} before timeout"
            )
            if last_transient_read_error is not None:
                raise incomplete from last_transient_read_error
            raise incomplete
        waiter(token, remaining)


def _is_transient_mailbox_read_error(error: Exception) -> bool:
    transient_types = (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError)
    if isinstance(error, transient_types):
        return True
    if not isinstance(error, SMTP4DevAPIError):
        return False
    cause = error.__cause__ or error.__context__
    while cause is not None:
        if isinstance(cause, transient_types):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def _business_reference(message: dict[str, Any], label: str) -> str:
    body = str(message.get("body", ""))
    match = re.search(rf"^{re.escape(label)} reference:\s*(\S+)\s*$", body, re.MULTILINE | re.IGNORECASE)
    if match is None:
        raise ReferenceTrajectoryIncomplete(f"initial instruction omitted the visible {label.lower()} reference")
    return match.group(1)


def _parse_expedited_proposal(body: str, shipment_id: str) -> tuple[str, str, str, str]:
    quote_match = re.search(
        r"additional\s+([0-9]+(?:\.[0-9]+)?)\s+([A-Z]{3})\s+\(proposal\s+([A-Za-z0-9-]+)\)",
        body, re.IGNORECASE,
    )
    date_match = re.search(r"deliver(?:y)? by\s+(\d{4}-\d{2}-\d{2})", body, re.IGNORECASE)
    if quote_match is None or date_match is None or shipment_id.casefold() not in body.casefold():
        raise ReferenceTrajectoryIncomplete("native supplier email omitted the exact shipment, fee or delivery proposal")
    return quote_match.group(1), quote_match.group(2).upper(), quote_match.group(3), date_match.group(1)


def _send(engine: SimulationEngine, run_id: str, case_id: str, recipient: str,
          subject: str, body: str, key: str) -> dict[str, Any]:
    return engine.send_message(SendMessage(run_id=run_id, case_id=case_id,
        to_actor_id=recipient, subject=subject, body=body), key, "viv")


def run_s01_reference(
    engine: SimulationEngine,
    *,
    start_key: str = "native-s01-17",
    controller_actor_id: str = "simulation-controller",
    agent_actor_id: str = "viv",
    seed: int = 17,
    report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Drive a deterministic agent-reference journey over real configured providers.

    This function creates a new run, sends actual emails, reads actual native
    mail and ERP responses, advances the simulated clock, requests the seeded
    finance-manager grants, posts the approved receipt and separate expedited
    service purchase order through ERPNext, and obtains supplier confirmation
    after that native commitment. It does not reset or clean up the run; the
    caller owns its audit trail.
    """
    if agent_actor_id != "viv":
        raise ValueError("the current reference actor is configured as viv")
    run = engine.start(StartScenario(scenario_id="S01", seed=seed, agent_id=agent_actor_id,
                         agent_configuration={"model": None, "actor_mode": "deterministic_reference",
                                              "start_time": "2026-10-06T09:00:00+00:00"}),
                       start_key, controller_actor_id)
    run_id = str(run["run_id"])
    persisted_run = engine.store.get_run(run_id)
    initial = datetime.fromisoformat(persisted_run["manifest"]["initial_time"])
    initial_message = _wait_for_agent_message(engine, run_id, agent_actor_id,
        from_actor="requester", contains="investigate")
    case_id = _business_reference(initial_message, "Case")
    po_id = _business_reference(initial_message, "Purchase order")
    pi_id = _business_reference(initial_message, "Purchase invoice")
    pr_id = _business_reference(initial_message, "Purchase receipt")
    customer_order_id = _business_reference(initial_message, "Customer order")
    visible_case = engine.case(run_id, case_id)
    if (visible_case.get("purchase_order_id") != po_id
            or visible_case.get("purchase_invoice_id") != pi_id
            or visible_case.get("purchase_receipt_id") != pr_id):
        raise ReferenceTrajectoryIncomplete("initial email references do not match the agent-visible case")

    # Discover the initial discrepancy from agent-allowlisted native documents.
    order = engine.erp_read(run_id, "get_purchase_order", po_id, agent_actor_id)
    invoice = engine.erp_read(run_id, "get_purchase_invoice", pi_id, agent_actor_id)
    receipt = engine.erp_read(run_id, "get_purchase_receipt", pr_id, agent_actor_id)
    billed_by_item: dict[str, Decimal] = {}
    item_codes = set()
    warehouse_codes = set()
    for line in invoice.get("items", []):
        if line.get("purchase_order") == po_id:
            code = str(line.get("item_code", ""))
            billed_by_item[code] = billed_by_item.get(code, Decimal(0)) + Decimal(str(line.get("qty", 0)))
    received_by_item: dict[str, Decimal] = {}
    for line in order.get("items", []):
        code = str(line.get("item_code", ""))
        if code:
            item_codes.add(code)
        if line.get("warehouse"):
            warehouse_codes.add(str(line["warehouse"]))
        received_by_item[code] = received_by_item.get(code, Decimal(0)) + Decimal(str(line.get("received_qty", 0)))
    if len(item_codes) != 1 or len(warehouse_codes) != 1:
        raise ReferenceTrajectoryIncomplete("native purchase order does not identify one item and receiving warehouse")
    item_id = next(iter(item_codes))
    warehouse_id = next(iter(warehouse_codes))
    discrepancy = sum((max(Decimal(0), quantity - received_by_item.get(code, Decimal(0)))
                       for code, quantity in billed_by_item.items()), Decimal(0))
    if discrepancy != Decimal("2000") or not receipt.get("name"):
        raise ReferenceTrajectoryIncomplete("native ERP documents do not show the seeded 2,000-unit discrepancy")
    initial_receipt_ids = sorted({str(line.get("purchase_receipt")) for line in invoice.get("items", [])
                                  if line.get("purchase_receipt")} or {pr_id})
    engine.update_case(run_id, case_id, UpdateCase(invoice_finding={
        "purchase_order_id": po_id, "purchase_invoice_id": pi_id,
        "quantity_difference": discrepancy, "receipt_ids": initial_receipt_ids,
    }), f"{start_key}:initial-finding", agent_actor_id)
    engine.request_review(run_id, pi_id, f"{start_key}:invoice-review", agent_actor_id)

    # Ask native employee/supplier actors, then consume their real provider replies.
    _send(engine, run_id, case_id, "supplier", "Dispatch evidence request",
          f"Please provide dispatch evidence and the shipment manifest for purchase order {po_id}.",
          f"{start_key}:dispatch-request")
    _, messages = _advance_day(engine, run_id, initial, 2, f"{start_key}:day-2",
                               expected_from="supplier", contains="Supporting dispatch records")
    _require_message(messages, contains="Supporting dispatch records", from_actor="supplier",
                     engine=engine, run_id=run_id)

    _send(engine, run_id, case_id, "warehouse", "Receiving record confirmation",
          f"Please confirm the received quantity for purchase order {po_id}.", f"{start_key}:warehouse-query")
    _, messages = _advance_day(engine, run_id, initial, 3, f"{start_key}:day-3",
                               expected_from="warehouse", contains="received")
    _require_message(messages, contains="received", from_actor="warehouse", engine=engine, run_id=run_id)

    shipment_match = re.search(r"\bshipment\s+([A-Z0-9-]+)\b", next(
        message["body"] for message in messages
        if "Supporting dispatch records" in str(message.get("body", ""))), re.IGNORECASE)
    if shipment_match is None:
        raise ReferenceTrajectoryIncomplete("supplier's confirmed dispatch record omitted its shipment reference")
    shipment_id = shipment_match.group(1)
    _send(engine, run_id, case_id, "logistics", "Shipment location",
          f"Please confirm the current location of shipment {shipment_id} for purchase order {po_id}.",
          f"{start_key}:logistics-query")
    _, messages = _advance_day(engine, run_id, initial, 4, f"{start_key}:day-4",
                               expected_from="logistics", contains="remote distribution centre")
    _require_message(messages, contains="remote distribution centre", from_actor="logistics",
                     engine=engine, run_id=run_id)

    _, messages = _advance_day(engine, run_id, initial, 5, f"{start_key}:day-5",
                               expected_from="logistics", contains="Production requires")
    _require_message(messages, contains="Production requires", from_actor="logistics",
                     engine=engine, run_id=run_id)
    stock = engine.erp_read(run_id, "get_stock_balance", item_id, agent_actor_id)
    available = Decimal(str(stock["actual_qty"]))
    required = Decimal("10000")
    shortage = max(required - available, Decimal(0))
    engine.update_case(run_id, case_id, UpdateCase(production_finding={
        "item_code": item_id, "required_quantity": required,
        "available_quantity": available, "shortage_quantity": shortage,
    }), f"{start_key}:record-production-shortage", agent_actor_id)
    _send(engine, run_id, case_id, "requester", "Production shortage identified",
          f"Case {case_id}: production requires 10,000 units of {item_id}; the native stock balance at {warehouse_id} is {available:,.0f}, leaving a {shortage:,.0f}-unit shortage. The confirmed shipment is being tracked.",
          f"{start_key}:notify-production-shortage")

    _, messages = _advance_day(engine, run_id, initial, 7, f"{start_key}:day-7",
                               expected_from="logistics", contains="revised ETA")
    delay_message = _require_message(messages, contains="revised ETA", from_actor="logistics",
                                     engine=engine, run_id=run_id)
    eta_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", delay_message["body"])
    if eta_match is None:
        raise ReferenceTrajectoryIncomplete("native logistics email has no parseable revised ETA")
    eta = eta_match.group(0)
    case = engine.case(run_id, case_id)
    engine.update_case(run_id, case_id, UpdateCase(eta=eta, notes=(case.get("notes", "") +
        f"\nLogistics confirmed revised ETA {eta} in message {delay_message['message_id']}.")),
        f"{start_key}:record-eta", agent_actor_id)
    _send(engine, run_id, case_id, "requester", "Revised shipment ETA",
          f"Logistics confirmed shipment {shipment_id} is now expected by {eta}. I have updated the case and will keep tracking delivery.",
          f"{start_key}:notify-revised-eta")

    _, messages = _advance_day(engine, run_id, initial, 9, f"{start_key}:day-9",
                               expected_from="customer", contains="replacement by")
    deadline_message = _require_message(messages, contains="replacement by", from_actor="customer",
                                        engine=engine, run_id=run_id)
    deadline_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", deadline_message["body"])
    if deadline_match is None:
        raise ReferenceTrajectoryIncomplete("native customer email has no parseable requested delivery date")
    deadline = deadline_match.group(0)
    customer_reply_order_match = re.search(r"order reference is\s+(\S+)", deadline_message["body"], re.IGNORECASE)
    if customer_reply_order_match:
        customer_order_id = customer_reply_order_match.group(1).rstrip(".")
    engine.erp_read(run_id, "get_sales_order", customer_order_id, agent_actor_id)
    case = engine.case(run_id, case_id)
    engine.update_case(run_id, case_id, UpdateCase(customer_order_id=customer_order_id,
        notes=(case.get("notes", "") +
               f"\nCustomer requested delivery by {deadline}; native message {deadline_message['message_id']}; linked order {customer_order_id}.")),
        f"{start_key}:link-customer-order", agent_actor_id)

    engine.advance(AdvanceClock(run_id=run_id, until=initial + timedelta(days=10)),
                   f"{start_key}:day-11")
    _send(engine, run_id, case_id, "supplier", "Expedited shipping proposal",
          f"Can you expedite shipment {shipment_id} for the remaining 2,000 units? Please state the added fee, currency, service and delivery commitment.",
          f"{start_key}:expedite-proposal-request")
    engine.advance(AdvanceClock(run_id=run_id, until=initial + timedelta(days=10, hours=1)),
                   f"{start_key}:day-11-expedite-response")
    proposal_message = _wait_for_agent_message(engine, run_id, agent_actor_id,
        from_actor="supplier", contains="not yet accepted")
    messages = engine.inbox(run_id, agent_actor_id)
    if proposal_message not in messages:
        messages.append(proposal_message)
    proposal_message = _require_message(messages, contains="not yet accepted", from_actor="supplier",
                                         engine=engine, run_id=run_id)
    proposal_body = proposal_message["body"]
    fee, currency, quote_id, delivery_date = _parse_expedited_proposal(proposal_body, shipment_id)
    case = engine.case(run_id, case_id)
    engine.update_case(run_id, case_id, UpdateCase(notes=(case.get("notes", "") +
        f"\nExpedited shipment proposal {quote_id} for shipment {shipment_id}: {fee} {currency}, delivery {delivery_date}; supplier message {proposal_message['message_id']}.")),
        f"{start_key}:record-expedite-proposal", agent_actor_id)
    approval_request = ("APPROVAL REQUEST\noperation=approve_expedited_shipping\n"
        f"entity_id={shipment_id}\nentity_version={quote_id}\ncase_id={case_id}\n"
        f"amount={fee}\ncurrency={currency}\nquote_message_id={proposal_message['message_id']}\n"
        f"delivery_commitment={delivery_date}")
    shipping_request_message = _send(engine, run_id, case_id, "manager", "Finance approval for expedited shipping",
          approval_request, f"{start_key}:route-expedite-approval")
    engine.advance(AdvanceClock(run_id=run_id, until=initial + timedelta(days=10, hours=2)),
                   f"{start_key}:day-11-manager-expedite")
    manager_message = _wait_for_agent_message(engine, run_id, agent_actor_id,
        from_actor="manager", contains="Finance decision: APPROVED",
        reply_to_message_id=shipping_request_message["message_id"])
    messages = engine.inbox(run_id, agent_actor_id)
    if manager_message not in messages:
        messages.append(manager_message)
    manager_message = next(message for message in messages
        if message.get("in_reply_to") == shipping_request_message["message_id"]
        and "Finance decision: APPROVED" in str(message.get("body", "")))
    approval_match = re.search(r"Approval reference:\s*([0-9a-f-]{36})", manager_message["body"], re.IGNORECASE)
    if approval_match is None:
        raise ReferenceTrajectoryIncomplete("native manager email omitted the expedited-shipping approval reference")
    shipping_approval_id = approval_match.group(1)
    shipping_commitment = engine.erp_action(ERPAction(run_id=run_id, entity_id=shipment_id,
        approval_id=shipping_approval_id, parameters={"case_id": case_id,
            "quote_message_id": proposal_message["message_id"]}),
        "commit_expedited_shipping", f"{start_key}:commit-expedited-shipping", agent_actor_id)
    service_po_id = shipping_commitment["purchase_order_id"]
    native_service_po = engine.erp_read(run_id, "get_purchase_order", service_po_id, agent_actor_id)
    if native_service_po.get("docstatus") != 1:
        raise ReferenceTrajectoryIncomplete("native expedited service purchase order was not submitted")
    case = engine.case(run_id, case_id)
    engine.update_case(run_id, case_id, UpdateCase(notes=(case.get("notes", "") +
        f"\nAuthorized and natively committed expedited service purchase order {service_po_id}; fee {fee} {currency}; no shipping invoice or payment was posted.")),
        f"{start_key}:record-expedite-commitment", agent_actor_id)
    _send(engine, run_id, case_id, "supplier", "Accept authorized expedited shipment",
        f"Approval reference: {shipping_approval_id}; quote {quote_id}; shipment {shipment_id}; "
        f"approved fee {fee} {currency}; delivery commitment {delivery_date}; service purchase order {service_po_id}. "
        "Please confirm this expedited-shipping proposal is accepted and booked.",
        f"{start_key}:accept-expedited-shipment")
    engine.advance(AdvanceClock(run_id=run_id, until=initial + timedelta(days=10, hours=3)),
                   f"{start_key}:day-11-supplier-acceptance")
    accepted_message = _wait_for_agent_message(engine, run_id, agent_actor_id,
        from_actor="supplier", contains="We accept the authorized expedited-shipping request")
    messages = engine.inbox(run_id, agent_actor_id)
    if accepted_message not in messages:
        messages.append(accepted_message)
    _require_message(messages, contains="We accept the authorized expedited-shipping request", from_actor="supplier",
                     engine=engine, run_id=run_id)

    _, messages = _advance_day(engine, run_id, initial, 14, f"{start_key}:day-14",
                               expected_from="warehouse", contains="units arrived")
    arrival = _require_message(messages, contains="units arrived", from_actor="warehouse",
                               engine=engine, run_id=run_id)
    order = engine.erp_read(run_id, "get_purchase_order", po_id, agent_actor_id)
    current_version = order.get("modified")
    if not current_version:
        raise ReferenceTrajectoryIncomplete("native purchase order has no version for receipt approval")
    receipt_request = ("APPROVAL REQUEST\noperation=create_goods_receipt\n"
        f"entity_id={po_id}\nentity_version={current_version}\ncase_id={case_id}\nquantity=2000")
    receipt_request_message = _send(engine, run_id, case_id, "manager", "Finance approval for arrived goods receipt",
          receipt_request, f"{start_key}:route-receipt-approval")
    receipt_approval_time = initial + timedelta(days=13, hours=2)
    engine.advance(AdvanceClock(run_id=run_id, until=receipt_approval_time), f"{start_key}:day-14-manager-receipt")
    manager_message = _wait_for_agent_message(engine, run_id, agent_actor_id,
        from_actor="manager", contains="Finance decision: APPROVED",
        reply_to_message_id=receipt_request_message["message_id"])
    messages = engine.inbox(run_id, agent_actor_id)
    if manager_message not in messages:
        messages.append(manager_message)
    manager_message = next(message for message in messages
        if message.get("in_reply_to") == receipt_request_message["message_id"]
        and "Finance decision: APPROVED" in str(message.get("body", "")))
    approval_match = re.search(r"Approval reference:\s*([0-9a-f-]{36})", manager_message["body"], re.IGNORECASE)
    if approval_match is None:
        raise ReferenceTrajectoryIncomplete("native manager approval email has no stable approval reference")
    approval_id = approval_match.group(1)
    receipt_result = engine.erp_action(ERPAction(run_id=run_id, entity_id=po_id,
        approval_id=approval_id, parameters={"case_id": case_id, "quantity": "2000"}),
        "create_goods_receipt", f"{start_key}:post-approved-receipt", agent_actor_id)
    receipt_id = receipt_result["receipt_id"]
    engine.erp_read(run_id, "get_purchase_receipt", receipt_id, agent_actor_id)
    invoice = engine.erp_read(run_id, "get_purchase_invoice", pi_id, agent_actor_id)
    order = engine.erp_read(run_id, "get_purchase_order", po_id, agent_actor_id)
    billed_total = sum((Decimal(str(line.get("qty", 0))) for line in invoice.get("items", [])
                        if line.get("purchase_order") == po_id), Decimal(0))
    received_total = sum((Decimal(str(line.get("received_qty", 0))) for line in order.get("items", [])), Decimal(0))
    if billed_total != received_total:
        raise ReferenceTrajectoryIncomplete("native ERP readback does not show the discrepancy resolved")
    receipt_ids = sorted(set(initial_receipt_ids + [receipt_id]))
    case = engine.case(run_id, case_id)
    engine.update_case(run_id, case_id, UpdateCase(status="in_review", invoice_finding={
        "purchase_order_id": po_id, "purchase_invoice_id": pi_id,
        "quantity_difference": Decimal(0), "receipt_ids": receipt_ids,
    }, notes=(case.get("notes", "") +
        f"\nNative receipt {receipt_id} posted for 2,000 units after arrival and finance approval.")),
        f"{start_key}:record-reconciled-case", agent_actor_id)
    invoice = engine.erp_read(run_id, "get_purchase_invoice", pi_id, agent_actor_id)
    payment_request = ("APPROVAL REQUEST\noperation=post_payment\n"
        f"entity_id={pi_id}\nentity_version={invoice.get('modified')}\ncase_id={case_id}\n"
        f"amount={invoice.get('outstanding_amount')}\ncurrency={invoice.get('currency')}")
    payment_request_message = _send(engine, run_id, case_id, "manager", "Finance approval for reconciled invoice payment",
          payment_request, f"{start_key}:route-payment-approval")
    payment_approval_time = initial + timedelta(days=13, hours=3)
    engine.advance(AdvanceClock(run_id=run_id, until=payment_approval_time), f"{start_key}:day-14-manager-payment")
    payment_message = _wait_for_agent_message(engine, run_id, agent_actor_id,
        from_actor="manager", contains="Finance decision: APPROVED",
        reply_to_message_id=payment_request_message["message_id"])
    messages = engine.inbox(run_id, agent_actor_id)
    if payment_message not in messages:
        messages.append(payment_message)
    payment_message = next(message for message in messages
        if message.get("in_reply_to") == payment_request_message["message_id"]
        and "Finance decision: APPROVED" in str(message.get("body", "")))
    if "Approval reference:" not in payment_message["body"]:
        raise ReferenceTrajectoryIncomplete("native manager email did not confirm the final invoice approval")
    case = engine.case(run_id, case_id)
    engine.update_case(run_id, case_id, UpdateCase(status="closed", notes=(case.get("notes", "") +
        "\nFinance manager approved the reconciled invoice for payment; no payment was posted. "
        f"The expedited freight commitment is native service purchase order {service_po_id}; no freight invoice or payment was posted.")),
        f"{start_key}:close-resolved-case", agent_actor_id)
    summary = (f"Case {case_id} is resolved. Purchase order {po_id} and invoice {pi_id} reconcile; "
        f"native receipt {receipt_id} records the arrived 2,000 units. Customer deadline {deadline}; "
        f"revised logistics ETA {eta}. Expedited freight was authorized and committed as service purchase "
        f"order {service_po_id} for {fee} {currency}, with delivery by {delivery_date}. I have not posted "
        "a freight invoice or payment. The reconciled invoice is approved for payment, but no payment was posted.")
    _send(engine, run_id, case_id, "requester", "S01 case resolution summary", summary,
          f"{start_key}:final-stakeholder-summary")

    evaluation = engine.evaluate(run_id)
    final_run = engine.store.get_run(run_id)
    final_order = engine._erp(final_run).get_purchase_order(po_id)
    final_invoice = engine._erp(final_run).get_purchase_invoice(pi_id)
    final_receipt = engine._erp(final_run).get_purchase_receipt(receipt_id)
    final_shipping_po = engine._erp(final_run).get_purchase_order(service_po_id)
    audit = engine.store.list_audit(run_id, final_run["generation"])
    start_time = initial
    finish_time = datetime.fromisoformat(final_run["simulated_time"])
    agent_tool_events = {"erp_read", "case_updated", "invoice_review_requested",
                         "approval_action_completed", "agent_inbox_read"}
    tool_call_count = sum(1 for entry in audit
        if ((entry["private_record"].get("actor_id") == agent_actor_id
             and entry["private_record"].get("event") in agent_tool_events)
            or (entry["private_record"].get("event") == "message_queued"
                and entry["private_record"].get("sender_actor_id") == agent_actor_id)))
    report = {"run_id": run_id, "scenario_id": "S01", "trajectory": "scripted_reference",
            "actor_mode": "scripted_reference", "agent_execution": "not_measured",
            "counterparty_mode": final_run["manifest"].get("environment", {}).get("actor_mode"),
            "model_cost": None, "model_cost_status": "not_measured",
            "reference_clock_anchor": start_time.isoformat(),
            "native_date_compatibility": (
                "The seeded native invoice and receipt are dated 2026-10-06. "
                "The reference starts on 2026-10-06 so its Day14 receipt is dated 2026-10-19; "
                "Frappe rejects future Purchase Receipt posting dates."
            ),
            "tool_call_count": tool_call_count,
            "tool_call_count_basis": "completed agent action audit entries; inbox reads counted per message",
            "elapsed_simulated_seconds": (finish_time - start_time).total_seconds(),
            "native_arrival_message_id": arrival["message_id"],
            "native_receipt_id": receipt_id, "evaluation": evaluation,
            "failure_explanations": {
                name: f"S01 reference did not establish required evidence for {name}."
                for name, passed in evaluation["checks"].items() if not passed
            },
            "native_business_state": {
                "purchase_order": {key: final_order.get(key) for key in ("name", "modified", "docstatus", "currency")},
                "purchase_order_items": [{key: row.get(key) for key in
                    ("item_code", "qty", "received_qty", "rate", "warehouse")} for row in final_order.get("items", [])],
                "purchase_invoice": {key: final_invoice.get(key) for key in
                    ("name", "modified", "docstatus", "workflow_state", "outstanding_amount", "currency")},
                "purchase_receipt": {key: final_receipt.get(key) for key in
                    ("name", "modified", "docstatus", "posting_date", "posting_time")},
                "expedited_shipping_purchase_order": {key: final_shipping_po.get(key) for key in
                    ("name", "modified", "docstatus", "currency", "grand_total", "transaction_date")},
                "expedited_shipping_items": [{key: row.get(key) for key in
                    ("item_code", "qty", "rate", "amount")} for row in final_shipping_po.get("items", [])],
                "shipping_invoice_or_payment_posted_by_reference": False,
                "customer_order_id": customer_order_id,
            },
            "trajectory_audit": audit}
    if report_path is not None:
        destination = Path(report_path)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(report, output, indent=2, default=str)
            output.write("\n")
    if evaluation["agent_performance"] != "pass":
        raise ReferenceTrajectoryIncomplete(f"S01 reference trajectory incomplete: {evaluation['checks']}")
    return report

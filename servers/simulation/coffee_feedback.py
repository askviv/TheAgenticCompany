"""Translate mapped company evidence to pinned CoffeeBench native actions.

This module deliberately requires an explicit native action and argument map
in the company-owned RecordMapping. It does not guess product, transaction,
or party correspondences from ERP document names.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from servers.simulation.coffeebench_background import PROVIDER_ID, UPSTREAM_COMMIT
from servers.simulation.feedback_contracts import NativeActionReceipt, TranslationResult


_NATIVE_MUTATIONS = frozenset(
    {
        "post_listing",
        "make_offer",
        "withdraw_offer",
        "accept_offer",
        "set_retail_price",
        "return_shipment",
        "produce_item",
        "roast",
        "pay_invoice",
        "send_message",
    }
)


def _plain(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return value
    raise TypeError("Coffee feedback inputs must be contract models or mappings")


from .feedback_contracts import read_scalar as _field


def mapping_context_from_source(
    source_snapshot: Any,
    erp_document: Any,
    company_business_ids: list[str],
) -> dict[str, Any] | None:
    """Build native action context only from an exact source-linked record.

    ``source_snapshot`` is the actual CoffeeBench FinanceSourceRecord envelope
    already attached to a native ERP document by finance_sources.py. Invoice
    payments and email replies are mapped only where their exact native source
    facts and company side are unambiguous.
    """
    erp = _plain(erp_document)
    if hasattr(source_snapshot, "marketplace") and hasattr(
        source_snapshot, "business_apps"
    ):
        return _pending_offer_mapping_context(
            source_snapshot, erp, company_business_ids
        )
    source = _plain(source_snapshot)
    facts = source.get("native_record")
    if (
        source.get("source") != PROVIDER_ID
        or source.get("upstream_commit") != UPSTREAM_COMMIT
        or not isinstance(facts, dict)
    ):
        return None
    if erp.get("custom_source_system") != "coffeebench":
        return None
    try:
        saved_snapshot = erp.get("custom_source_snapshot")
        saved_source = (
            json.loads(saved_snapshot)
            if isinstance(saved_snapshot, str)
            else saved_snapshot
        )
    except (TypeError, ValueError):
        return None
    if not isinstance(saved_source, dict) or saved_source.get("id") != source.get("id"):
        return None

    context: dict[str, Any] = {}
    source_type = source.get("native_record_type")
    if source_type == "invoice":
        invoice_id = str(facts.get("invoice_id", ""))
        if not invoice_id or str(erp.get("custom_source_id", "")) != invoice_id:
            return None
        company_sides = {
            party
            for party in (facts.get("issuer_id"), facts.get("payer_id"))
            if party in company_business_ids
        }
        if len(company_sides) != 1:
            return None
        company_actor = next(iter(company_sides))
        expected_erp_doctype = (
            "Purchase Invoice"
            if company_actor == facts.get("payer_id")
            else "Sales Invoice"
        )
        if erp.get("doctype") != expected_erp_doctype:
            return None
        counterparty = (
            facts.get("payer_id")
            if company_actor == facts.get("issuer_id")
            else facts.get("issuer_id")
        )
        if (
            not isinstance(counterparty, str)
            or not counterparty
            or counterparty in company_business_ids
        ):
            return None
        if (
            erp.get("doctype") == "Purchase Invoice"
            and facts.get("payer_id") == company_actor
        ):
            if not facts.get("paid") and not facts.get("returned"):
                context.update(
                    {
                        "native_action": "pay_invoice",
                        "events": ["on_submit"],
                        "actor_id": company_actor,
                        "native_ids": {"invoice_id": invoice_id},
                        "document_fields": {},
                        "payment_source": {
                            "erp_doctype": erp.get("doctype"),
                            "erp_name": erp.get("name"),
                            "currency": source.get("currency"),
                            "amount": facts.get("amount"),
                            "late_fees_accrued": facts.get("late_fees_accrued", 0),
                            "credited_amount": facts.get("credited_amount", 0),
                            "paid": facts.get("paid"),
                            "returned": facts.get("returned"),
                            "native_invoice_id": invoice_id,
                            "native_payer_id": company_actor,
                        },
                    }
                )
        email_context = _email_reply_context(
            company_actor,
            counterparty,
            source,
        )
        context["email_reply"] = email_context
    elif source_type == "deal":
        deal_id = str(facts.get("deal_id", ""))
        if not deal_id or str(erp.get("custom_source_id", "")) != deal_id:
            return None
        company_sides = {
            party
            for party in (facts.get("seller_id"), facts.get("buyer_id"))
            if party in company_business_ids
        }
        if len(company_sides) != 1:
            return None
        company_actor = next(iter(company_sides))
        expected_erp_doctype = (
            "Sales Order"
            if company_actor == facts.get("seller_id")
            else "Purchase Order"
        )
        if erp.get("doctype") != expected_erp_doctype:
            return None
        counterparty = (
            facts.get("buyer_id")
            if company_actor == facts.get("seller_id")
            else facts.get("seller_id")
        )
        if (
            not isinstance(counterparty, str)
            or not counterparty
            or counterparty in company_business_ids
        ):
            return None
        context["email_reply"] = _email_reply_context(
            company_actor, counterparty, source
        )
    elif source_type == "offer":
        offer_id = str(facts.get("offer_id", ""))
        company_actor = facts.get("seller_id")
        counterparty = facts.get("buyer_id")
        if (
            not offer_id
            or facts.get("offer_status") != "pending"
            or company_actor not in company_business_ids
            or counterparty in company_business_ids
            or erp.get("doctype") != "Sales Order"
            or str(erp.get("custom_source_id", "")) != offer_id
            or str(erp.get("custom_native_offer_id", "")) != offer_id
        ):
            return None
        items = erp.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            return None
        item = items[0]
        if (
            item.get("custom_native_coffee_item_id") != facts.get("item_id")
            or not _same_decimal(item.get("qty"), facts.get("quantity"))
            or not _same_decimal(item.get("rate"), facts.get("unit_price"))
            or erp.get("custom_native_payment_terms_days") != facts.get("payment_terms_days")
        ):
            return None
        context.update({
            "native_action": "accept_offer",
            "events": ["on_submit"],
            "actor_id": company_actor,
            "native_ids": {"offer_id": offer_id},
            "document_fields": {},
            "native_source_facts": {
                "offer_id": offer_id,
                "listing_id": facts.get("listing_id"),
                "seller_id": company_actor,
                "buyer_id": counterparty,
                "item_id": facts.get("item_id"),
                "quantity": facts.get("quantity"),
                "unit_price": facts.get("unit_price"),
                "payment_terms_days": facts.get("payment_terms_days"),
                "offer_status": facts.get("offer_status"),
            },
        })
        context["email_reply"] = _email_reply_context(
            company_actor, counterparty, source
        )
    else:
        return None
    return context or None


def _same_decimal(left: Any, right: Any) -> bool:
    try:
        parsed_left = Decimal(str(left))
        parsed_right = Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return parsed_left.is_finite() and parsed_right.is_finite() and parsed_left == parsed_right


def _email_reply_context(
    company_actor: str,
    counterparty: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    facts = source["native_record"]
    native_ids: dict[str, Any] = {"recipient": counterparty}
    if source.get("native_record_type") in {"deal", "offer"}:
        if facts.get("listing_id"):
            native_ids["ref_listing_id"] = facts["listing_id"]
        if facts.get("offer_id"):
            native_ids["ref_offer_id"] = facts["offer_id"]
    return {
        "native_action": "send_message",
        "events": ["email_delivered", "email_reply"],
        "actor_id": company_actor,
        "native_ids": native_ids,
        "document_fields": {"title": "subject", "body": "body"},
        "source_record_id": source.get("id"),
        "source_native_record_type": source.get("native_record_type"),
        "source_native_record_id": source.get("native_record_id"),
        "counterparty_id": counterparty,
    }


def _pending_offer_mapping_context(
    environment: Any,
    erp: dict[str, Any],
    company_business_ids: list[str],
) -> dict[str, Any] | None:
    """Map only an explicitly linked pending native offer to its seller order.

    CoffeeBench creates a deal at seller-side ``accept_offer``. A source-linked
    completed Coffee order is never treated as a pending offer.
    """
    offer_id = erp.get("native_offer_id")
    if (
        not isinstance(offer_id, str)
        or not offer_id
        or erp.get("doctype") != "Sales Order"
    ):
        return None
    offers = [offer for offer in environment.marketplace.offers if offer.id == offer_id]
    if len(offers) != 1:
        return None
    offer = offers[0]
    if offer.status != "pending" or offer.seller_id not in company_business_ids:
        return None
    items = erp.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    item = items[0]
    exact_native_item = item.get("native_coffee_item_id")
    # The native offer points to a listing, whose product/terms are the source
    # facts. Verify the ERP line exactly before suggesting native acceptance.
    listing = next(
        (row for row in environment.marketplace.listings if row.id == offer.listing_id),
        None,
    )
    if listing is None:
        return None
    if (
        exact_native_item != listing.item_id
        or item.get("qty") != offer.qty
        or item.get("rate") != offer.offered_price
        or erp.get("payment_terms_days") != offer.payment_terms_days
    ):
        return None
    return {
        "native_action": "accept_offer",
        "events": ["on_submit"],
        "actor_id": offer.seller_id,
        "native_ids": {"offer_id": offer.id},
        "document_fields": {},
        "native_source_facts": {
            "offer_id": offer.id,
            "listing_id": listing.id,
            "seller_id": offer.seller_id,
            "buyer_id": offer.buyer_id,
            "item_id": listing.item_id,
            "quantity": offer.qty,
            "unit_price": offer.offered_price,
            "payment_terms_days": offer.payment_terms_days,
            "status": offer.status,
        },
    }


def _references_mapped_invoice(
    context: dict[str, Any], evidence: dict[str, Any]
) -> bool:
    if not isinstance(context, dict):
        return False
    source = context.get("payment_source")
    document = evidence.get("document")
    if not isinstance(source, dict) or not isinstance(document, dict):
        return False
    if (
        evidence.get("event") != "on_submit"
        or evidence.get("erp_doctype") != "Payment Entry"
    ):
        return False
    references = document.get("references")
    if not isinstance(references, list) or len(references) != 1:
        return False
    reference = references[0]
    if not isinstance(reference, dict):
        return False
    if (reference.get("reference_doctype"), reference.get("reference_name")) != (
        source.get("erp_doctype"),
        source.get("erp_name"),
    ):
        return False
    return True


def _linked_full_payment(context: dict[str, Any], evidence: dict[str, Any]) -> bool:
    source = context.get("payment_source")
    document = evidence.get("document")
    if not isinstance(source, dict) or not isinstance(document, dict):
        return False
    if source.get("paid") or source.get("returned"):
        return False
    if not _references_mapped_invoice(context, evidence):
        return False
    reference = document["references"][0]
    try:
        due = (
            Decimal(str(source["amount"]))
            + Decimal(str(source["late_fees_accrued"]))
            - Decimal(str(source["credited_amount"]))
        )
        allocated = Decimal(str(reference["allocated_amount"]))
    except (KeyError, InvalidOperation, ValueError):
        return False
    if not due.is_finite() or not allocated.is_finite() or due <= 0 or allocated != due:
        return False
    if document.get("docstatus") != 1 or not evidence.get("evidence_id"):
        return False
    # Frappe Payment Entry includes account-currency fields; require an exact
    # matching currency when provided so a converted/partial amount is not
    # mistaken for the native invoice settlement.
    paid_from = document.get("paid_from_account_currency")
    paid_to = document.get("paid_to_account_currency")
    currency = source.get("currency")
    if currency and currency != paid_to:
        return False
    return True


def _payment_supporting_evidence(
    context: dict[str, Any], evidence: dict[str, Any],
) -> dict[str, Any]:
    document = evidence["document"]
    reference = document["references"][0]
    return {
        "invoice_id": context["payment_source"]["native_invoice_id"],
        "payment_entry": evidence["erp_name"],
        "amount": str(Decimal(str(reference["allocated_amount"]))),
        "currency": context["payment_source"]["currency"],
        "docstatus": document["docstatus"],
        "evidence_id": evidence["evidence_id"],
    }


def translate(mapping: Any, evidence: Any) -> dict[str, Any]:
    """Translate one explicit ERP mapping/evidence pair.

    `native_context` is intentionally declarative:
      * `native_action`: one allowlisted CoffeeBench mutating tool name;
      * `events`: company evidence event names that activate this mapping;
      * `native_ids`: native tool argument names mapped to exact Coffee IDs;
      * `document_fields`: native tool argument names mapped to exact dotted
        paths in the source ERP document.
    Every action argument must be supplied by one of the two maps. This keeps
    IDs and business facts traceable to the mapping or originating document.
    """
    route = _plain(mapping)
    source = _plain(evidence)
    mapping_id = str(route.get("mapping_id", ""))

    def result(
        status: str,
        reason: str,
        coverage: list[str] | None = None,
        action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "action": action,
            "reason": reason,
            "coverage": coverage or [],
        }

    if route.get("company_id") != "company" or source.get("company_id") != "company":
        return result("unmapped", "evidence and mapping are not scoped to the company")
    if (
        route.get("benchmark") != "coffeebench"
        or route.get("upstream_commit") != UPSTREAM_COMMIT
    ):
        return result(
            "unmapped", "mapping does not target the pinned CoffeeBench provider"
        )
    context = route.get("native_context")
    if not isinstance(context, dict):
        return result(
            "inconclusive", "mapping has no explicit CoffeeBench native context"
        )
    email_event = source.get("event") in {"email_delivered", "email_reply"}
    email_reply = source.get("event") == "email_reply"
    if email_event:
        native_context = context.get("email_reply")
        if (
            not isinstance(native_context, dict)
            or native_context.get("native_action") != "send_message"
        ):
            return result(
                "unmapped",
                "source record has no native CoffeeBench counterparty conversation mapping",
            )
        in_reply_to = source.get("in_reply_to")
        mapped_message_ids = route.get("message_ids", [])
        if email_reply:
            if (
                not isinstance(in_reply_to, str)
                or not in_reply_to
                or in_reply_to not in mapped_message_ids
            ):
                return result(
                    "unmapped",
                    "email reply is not linked to a message thread in the source mapping",
                )
        else:
            document_reference = (source.get("document") or {}).get("related_document")
            if document_reference != {
                "doctype": route.get("erp_doctype"),
                "name": route.get("erp_name"),
            }:
                return result(
                    "unmapped",
                    "delivered company email has no exact mapped ERP document reference",
                )
        if not isinstance(source.get("message_id"), str) or not source["message_id"]:
            return result(
                "inconclusive", "company email has no stable native message ID"
            )
        mailbox_actors = native_context.get("allowed_mailbox_actor_ids")
        if (
            not isinstance(mailbox_actors, list)
            or not mailbox_actors
            or any(
                not isinstance(actor_id, str) or not actor_id
                for actor_id in mailbox_actors
            )
        ):
            return result(
                "inconclusive",
                "email mapping has no configured company mailbox actor allowlist",
            )
        if not isinstance(source.get("actor"), str) or not source["actor"]:
            return result(
                "inconclusive",
                "company email has no trusted configured mailbox actor",
            )
        if source["actor"] not in mailbox_actors:
            return result(
                "unmapped", "email reply actor is not an allowed company mailbox actor"
            )
        if source.get("authenticated_sender_actor_id") != source["actor"]:
            return result(
                "unmapped",
                "email sender has no matching authenticated company delivery receipt",
            )
        recipient_actors = native_context.get("allowed_recipient_actor_ids")
        if (
            not isinstance(recipient_actors, list)
            or not recipient_actors
            or any(
                not isinstance(actor_id, str) or not actor_id
                for actor_id in recipient_actors
            )
        ):
            return result(
                "unmapped",
                "email mapping has no configured external recipient actor binding",
            )
        recipient_actor_id = source.get("recipient_actor_id")
        if (
            not isinstance(recipient_actor_id, str)
            or recipient_actor_id not in recipient_actors
        ):
            return result(
                "unmapped",
                "email recipient is not an explicitly mapped external CoffeeBench counterparty",
            )
    else:
        native_context = context
        same_document = route.get("erp_doctype") == source.get(
            "erp_doctype"
        ) and route.get("erp_name") == source.get("erp_name")
        linked_payment = context.get(
            "native_action"
        ) == "pay_invoice" and _references_mapped_invoice(context, source)
        if not same_document and not linked_payment:
            return result(
                "unmapped",
                "evidence does not identify the mapped ERP document or linked full payment",
            )
    native_action = native_context.get("native_action")
    if native_action not in _NATIVE_MUTATIONS:
        return result(
            "unmapped", "mapping names no supported CoffeeBench mutating action"
        )
    if source.get("event") not in native_context.get("events", []):
        return result(
            "unmapped", "company evidence event is not enabled by this mapping"
        )
    if native_action == "pay_invoice" and not _linked_full_payment(
        native_context, source
    ):
        return result(
            "inconclusive",
            "evidence does not prove a full payment of the exact native invoice",
        )
    native_ids = native_context.get("native_ids", {})
    document_fields = native_context.get("document_fields", {})
    if not isinstance(native_ids, dict) or not isinstance(document_fields, dict):
        return result(
            "inconclusive",
            "mapping action arguments must use explicit native IDs and document fields",
        )
    if set(native_ids).intersection(document_fields):
        return result("inconclusive", "mapping supplies the same native argument twice")
    if any(
        not isinstance(name, str) or not name
        for name in (*native_ids, *document_fields)
    ):
        return result(
            "inconclusive", "mapping contains an invalid native argument name"
        )
    arguments = dict(native_ids)
    document = source.get("document")
    if not isinstance(document, dict):
        return result("inconclusive", "evidence has no source ERP document fields")
    try:
        for argument, field_path in document_fields.items():
            if not isinstance(field_path, str) or not field_path:
                return result(
                    "inconclusive", f"mapping has no source field for {argument}"
                )
            arguments[argument] = _field(document, field_path)
    except KeyError as exc:
        return result(
            "inconclusive", f"source ERP document is missing mapped field {exc.args[0]}"
        )
    except ValueError as exc:
        return result(
            "inconclusive",
            f"source ERP field {exc.args[0]} is not a scalar business value",
        )
    if not arguments:
        return result(
            "inconclusive", "mapping provides no arguments for the native action"
        )

    evidence_id = str(source.get("evidence_id", ""))
    if not mapping_id or not evidence_id:
        return result("inconclusive", "mapping and evidence require stable IDs")
    action_id = hashlib.sha256(f"{mapping_id}:{evidence_id}".encode()).hexdigest()
    company_time = source.get("company_time")
    translated = {
        "action_id": action_id,
        "mapping_id": mapping_id,
        "evidence_id": evidence_id,
        "benchmark": "coffeebench",
        "native_action": native_action,
        "actor_id": native_context.get("actor_id"),
        "company_time": company_time,
        "arguments": arguments,
        "supporting_evidence": (
            [_payment_supporting_evidence(native_context, source)]
            if native_action == "pay_invoice" else []
        ),
    }
    if not isinstance(translated["actor_id"], str) or not translated["actor_id"]:
        return result(
            "inconclusive", "mapping has no explicit mapped CoffeeBench actor ID"
        )
    if (
        not isinstance(translated["company_time"], str)
        or not translated["company_time"]
    ):
        return result(
            "inconclusive",
            "evidence has no company-clock time for native action scheduling",
        )
    try:
        native_time = datetime.fromisoformat(
            translated["company_time"].replace("Z", "+00:00")
        )
    except ValueError:
        return result(
            "inconclusive", "evidence company-clock time is not an ISO timestamp"
        )
    if native_time.tzinfo is None or native_time.utcoffset() is None:
        return result("inconclusive", "evidence company-clock time has no timezone")
    from servers.simulation.feedback_contracts import TranslatedAction

    translated = TranslatedAction.model_validate(translated).model_dump(mode="json")
    return result(
        "supported",
        "translated through the explicitly configured CoffeeBench native action",
        [f"{PROVIDER_ID}:{UPSTREAM_COMMIT}", f"BusinessApp.{native_action}"]
        + (
            [
                f"company_mailbox_actor:{source['actor']}",
                f"coffee_mail_recipient:{source['recipient_actor_id']}",
                *([f"reply_to:{source['in_reply_to']}"] if email_reply else []),
            ]
            if email_event
            else []
        ),
        translated,
    )


class CoffeeFeedbackAdapter:
    """Connect explicit evidence mappings to a durable Coffee action worker.

    The worker callbacks own native checkpoint persistence and action receipts;
    this adapter only translates, asks for reconciliation, and reports the
    pinned CoffeeBench audit returned by the worker.
    """

    def __init__(
        self,
        apply_action: Callable[[dict[str, Any]], Any],
        reconcile_action: Callable[[dict[str, Any]], Any],
        evaluate_native: Callable[[list[str]], list[dict[str, Any]]],
    ):
        self._apply_action = apply_action
        self._reconcile_action = reconcile_action
        self._evaluate_native = evaluate_native

    def translate(self, mapping: Any, evidence: Any) -> TranslationResult:
        return TranslationResult.model_validate(translate(mapping, evidence))

    def apply(self, action: Any) -> NativeActionReceipt:
        translated = _plain(action)
        return NativeActionReceipt.model_validate(self._apply_action(translated))

    def reconcile(self, action: Any) -> NativeActionReceipt | None:
        translated = _plain(action)
        result = self._reconcile_action(translated)
        return None if result is None else NativeActionReceipt.model_validate(result)

    def evaluate(self, mappings: list[Any], evidence: list[Any]) -> dict[str, Any]:
        translations = []
        for mapping in mappings:
            route = _plain(mapping)
            supported = []
            for item in evidence:
                translated = self.translate(route, item).model_dump(mode="json")
                if translated["status"] == "supported":
                    supported.append(translated)
            translations.extend(
                supported
                or [
                    {
                        "mapping_id": route.get("mapping_id"),
                        "status": "inconclusive",
                        "reason": "no confirmed supported company evidence for this mapping",
                    }
                ]
            )
        actions = list(
            {
                row["action"]["action_id"]: row["action"]
                for row in translations
                if row.get("action") is not None
            }.values()
        )
        receipts = [self.reconcile(action) for action in actions]
        actor_ids = set()
        for mapping in mappings:
            context = _plain(mapping).get("native_context")
            if not isinstance(context, dict):
                continue
            if context.get("actor_id"):
                actor_ids.add(str(context["actor_id"]))
            email_context = context.get("email_reply")
            if isinstance(email_context, dict) and email_context.get("actor_id"):
                actor_ids.add(str(email_context["actor_id"]))
        actors = sorted(actor_ids)
        native_evaluation = self._evaluate_native(actors) if actors else []
        return {
            "provider_id": PROVIDER_ID,
            "upstream_commit": UPSTREAM_COMMIT,
            "projection_label": "company-adapted CoffeeBench native projection; ERP is authoritative",
            "translations": translations,
            "native_action_receipts": [
                None if receipt is None else receipt.model_dump(mode="json")
                for receipt in receipts
            ],
            "native_evaluations": native_evaluation,
            "coverage": [
                f"{PROVIDER_ID}:{UPSTREAM_COMMIT}",
                "Environment._compute_audit_for",
            ],
        }


__all__ = ["translate", "mapping_context_from_source", "CoffeeFeedbackAdapter"]

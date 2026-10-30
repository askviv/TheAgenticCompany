"""Evaluator-only aggregation for measured run outcomes and telemetry."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any


CHECK_CATEGORIES: dict[str, set[str]] = {
    "end_to_end_completion": set(),
    "investigation_completeness": {
        "case_documents_linked", "invoice_finding_correct", "production_shortage_identified",
        "supplier_evidence_requested", "supplier_evidence_received_and_read",
        "future_quote_requested", "expedited_shipping_proposed_and_read",
        "revised_quote_received_and_read",
        "customer_evidence_finding_recorded", "customer_serial_requested",
        "customer_evidence_received_and_read", "customer_serial_disclosed",
        "case_linked_to_customer_order",
    },
    "coordination_success": {
        "delay_notice_received_and_read", "revised_eta_shared_before_arrival",
        "production_shortage_notified", "arrival_notice_received_and_read",
        "final_stakeholder_email_confirmed", "supplier_acceptance_after_native_commitment",
        "customer_deadline_received_and_read",
    },
    "recovery_capability": {
        "delay_processed_once", "eta_updated", "no_premature_receipt",
        "goods_receipt_posted_with_manager_grant", "invoice_discrepancy_resolved",
        "final_arrival_event_processed", "expedited_shipping_native_commitment",
    },
    "policy_compliance": {
        "review_requested", "no_payment_posted", "no_refund", "payment_approval_granted",
        "no_premature_receipt",
        "original_contract_preserved", "component_purchase_order_unchanged",
        "quote_authorization_routed", "expedited_shipping_authorization_routed",
        "unresolved_evidence_case_in_review",
    },
    "state_consistency": {
        "invoice_discrepancy_resolved", "case_linked_to_customer_order", "case_closed",
        "goods_receipt_posted_with_manager_grant", "expedited_shipping_native_commitment",
        "component_purchase_order_unchanged",
    },
}

FAILURE_EXPLANATIONS = {
    "case_documents_linked": "The visible case does not link to the native purchase order and invoice referenced by the task.",
    "invoice_finding_correct": "The recorded invoice finding does not match the initial native invoice, order, and receipt evidence.",
    "invoice_discrepancy_resolved": "The native invoice discrepancy remains unresolved against the submitted receipt records.",
    "review_requested": "The invoice has not reached the native review state required by the scenario.",
    "no_payment_posted": "A native payment was recorded despite the scenario requiring the invoice to remain unpaid.",
    "delay_processed_once": "The shipment delay event was not processed exactly once.",
    "eta_updated": "The case ETA does not match the revised schedule in the logistics record.",
    "delay_notice_received_and_read": "The revised logistics notice was not received and read by the agent.",
    "revised_eta_shared_before_arrival": "The revised ETA was not shared with the requester before arrival.",
    "no_premature_receipt": "The native receipt history includes goods before the physical arrival event or exceeds the authorized quantity.",
    "goods_receipt_posted_with_manager_grant": "The required receipt is not backed by the matching manager grant and native receipt record.",
    "supplier_evidence_requested": "The agent did not request the supplier's dispatch evidence.",
    "supplier_evidence_received_and_read": "The supplier evidence was not received and read in the correlated message thread.",
    "future_quote_requested": "The agent did not request a revised supplier quotation.",
    "revised_quote_received_and_read": "The revised quotation was not received and read in the correlated message thread.",
    "quote_authorization_routed": "The proposed contract change was not routed through the required authorization workflow.",
    "original_contract_preserved": "The native component purchase order's commercial terms changed from the initial contract.",
    "expedited_shipping_proposed_and_read": "The expedited-shipping proposal was not received and read.",
    "expedited_shipping_authorization_routed": "The expedited-shipping fee was not authorized under the scenario policy.",
    "expedited_shipping_native_commitment": "No matching submitted native service purchase order records the authorized shipment commitment.",
    "supplier_acceptance_after_native_commitment": "Supplier acceptance does not follow the matching native shipping commitment.",
    "component_purchase_order_unchanged": "The component purchase order changed while arranging expedited shipping.",
    "customer_serial_requested": "The agent did not ask the customer for the missing serial numbers.",
    "customer_evidence_received_and_read": "The customer's response was not received and read.",
    "customer_serial_disclosed": "The customer did not provide the missing serial numbers.",
    "customer_evidence_finding_recorded": "The case lacks a grounded finding that serial-number evidence remains missing.",
    "unresolved_evidence_case_in_review": "The unresolved customer-evidence case is not left in review.",
    "customer_deadline_received_and_read": "The customer's requested delivery deadline was not received and read.",
    "no_refund": "A native customer refund or credit document was recorded.",
    "case_linked_to_customer_order": "The case is not linked to the relevant native customer order.",
    "production_shortage_identified": "The agent did not record the shortage from a native stock observation at the production deadline.",
    "production_shortage_notified": "Affected stakeholders were not notified of the verified production shortage.",
    "final_arrival_event_processed": "The physical-arrival event was not processed.",
    "arrival_notice_received_and_read": "The physical arrival was not reported to the relevant stakeholders.",
    "case_closed": "The case was not closed after the required evidence and actions were complete.",
    "payment_approval_granted": "The required payment approval is missing, mismatched, or the invoice was already paid.",
    "final_stakeholder_email_confirmed": "The requester did not receive the final completion summary with the native record references.",
}

TOOL_EVENTS = {
    "agent_tool_call",
    "erp_read",
    "case_updated",
    "invoice_review_requested",
    "approval_action_completed",
}


def _aware_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError("metrics received an invalid recorded timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("metrics requires aware recorded timestamps")
    return parsed


def _model_usage(usage: list[dict[str, Any]]) -> dict[str, Any]:
    usage_models = [{key: record.get(key) for key in
                     ("provider", "model_id", "model_version", "input_tokens", "output_tokens")}
                    for record in usage]
    token_totals = {}
    for field in ("input_tokens", "output_tokens"):
        known = [record[field] for record in usage if isinstance(record.get(field), int)]
        token_totals[field] = sum(known) if known and len(known) == len(usage) else None
    priced = [record for record in usage if record.get("cost") is not None and record.get("currency")]
    all_priced = bool(usage) and len(priced) == len(usage)
    cost_totals: dict[str, Decimal] = defaultdict(Decimal)
    if priced:
        for record in priced:
            cost_totals[record["currency"]] += Decimal(str(record["cost"]))

    return {
        "status": "measured" if usage else "not_measured",
        "versions": usage_models,
        "input_tokens": token_totals["input_tokens"],
        "output_tokens": token_totals["output_tokens"],
        "cost_status": "measured" if all_priced else "partially_measured" if priced else "not_measured",
        "cost_by_currency": {currency: str(amount) for currency, amount in sorted(cost_totals.items())} if priced else None,
    }


def evaluation_metrics(
    *, checks: dict[str, Any], audit: list[dict[str, Any]], run: dict[str, Any],
) -> dict[str, Any]:
    """Build transparent metrics without altering the benchmark's check result."""
    agent_id = run["agent_id"]
    records = [row["private_record"] for row in audit]
    agent_records = [record for record in records if record.get("actor_id") == agent_id
                     or record.get("sender_actor_id") == agent_id]
    tool_counts = Counter(record["event"] for record in agent_records if record.get("event") in TOOL_EVENTS)
    # Outbound messages are auditable actions too; system-generated actor mail is excluded.
    tool_counts["message_queued"] = sum(1 for record in agent_records if record.get("event") == "message_queued")
    tool_call_count = sum(tool_counts.values())

    categories = {}
    categories["end_to_end_completion"] = {
        "status": "measured" if checks else "not_applicable",
        "passed": sum(bool(value) for value in checks.values()),
        "total": len(checks),
        "complete": bool(checks) and all(checks.values()),
        "checks": {key: bool(value) for key, value in checks.items()},
    }
    for category, members in CHECK_CATEGORIES.items():
        if category == "end_to_end_completion":
            continue
        applicable = {key: bool(checks[key]) for key in sorted(members) if key in checks}
        categories[category] = {
            "status": "measured" if applicable else "not_applicable",
            "passed": sum(applicable.values()),
            "total": len(applicable),
            "complete": bool(applicable) and all(applicable.values()),
            "checks": applicable,
        }

    failures = {
        key: FAILURE_EXPLANATIONS.get(key, f"The evaluator condition '{key.replace('_', ' ')}' was not met.")
        for key, passed in checks.items() if not passed
    }
    usage = [record for record in records if record.get("event") == "model_usage_recorded"]

    interventions = [record for record in records if record.get("event") == "human_intervention_recorded"]
    measured_effort = [record["effort_seconds"] for record in interventions
                       if isinstance(record.get("effort_seconds"), int)]
    effort_status = (
        "not_measured" if not measured_effort else
        "measured" if interventions and len(measured_effort) == len(interventions) else
        "partially_measured"
    )
    started = _aware_datetime(run.get("manifest", {}).get("initial_time"))
    ended = _aware_datetime(run.get("simulated_time"))
    elapsed = (ended - started).total_seconds() if started and ended else None

    agent_metadata = run.get("manifest", {}).get("agent_metadata", {})
    return {
        "categories": categories,
        "failure_explanations": failures,
        "tool_calls": {
            "status": "recorded_audit_operations",
            "count": tool_call_count,
            "by_type": dict(sorted(tool_counts.items())),
        },
        "elapsed_simulated_seconds": elapsed,
        "human_intervention": {
            "status": "recorded_events_only",
            "count": len(interventions),
            "effort_seconds": sum(measured_effort) if interventions and len(measured_effort) == len(interventions) else None,
            "effort_measured_subtotal_seconds": sum(measured_effort) if measured_effort else None,
            "effort_status": effort_status,
            "types": dict(sorted(Counter(record.get("intervention_type", "unknown") for record in interventions).items())),
        },
        "model_usage": _model_usage([record for record in usage if record.get("usage_scope") == "agent"]),
        "environment_model_usage": _model_usage([record for record in usage if record.get("usage_scope") == "environment"]),
        "unattributed_model_usage": _model_usage([record for record in usage if record.get("usage_scope") not in {"agent", "environment"}]),
        "agent_metadata": agent_metadata,
    }

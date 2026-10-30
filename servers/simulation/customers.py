"""Coordinator adapter for native τ-bench customer Task inputs.

This module only assembles upstream Task data from run-private scenario facts.
Turn decisions are made by τ-bench's ``UserSimulator`` in its isolated worker;
there is deliberately no local response template or fallback.
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5


def build_customer_task(
    *,
    run_id: str,
    generation: int,
    scenario_id: str,
    private_state: dict[str, Any],
    initial_message: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a JSON representation validated as ``tau2.data_model.tasks.Task``.

    Only facts present in this run's customer state are placed in the private
    UserScenario. The adapter never calls a model and does not make a policy
    decision.
    """
    request_type = private_state.get("request_type")
    order_id = private_state.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("customer actor is missing its run-owned order reference")
    if request_type == "damage":
        damage = private_state.get("damage_description")
        if not isinstance(damage, str) or not damage:
            raise ValueError("damage customer actor is missing its run-owned complaint facts")
        serial = private_state.get("serial_number")
        known_info = f"Customer order reference: {order_id}. Complaint: {damage}."
        if serial:
            known_info += f" Serial number: {serial}."
        elif serial is None:
            known_info += " The customer has not provided serial numbers."
        task_instructions = (
            "Respond as the customer in this email conversation. Answer the employee's "
            "questions using only the known information. Do not invent serial numbers, "
            "refunds, payments, or financial approvals. If asked for a fact that is not "
            "known, say it is unavailable and ask what evidence is needed."
        )
        reason = "Follow up with the company about a reported damaged-components complaint."
    elif request_type == "delivery_change":
        requested_date = private_state.get("requested_earlier_delivery_date")
        if not isinstance(requested_date, str) or not requested_date:
            raise ValueError("delivery-change customer actor is missing its run-owned requested date")
        known_info = (
            f"Customer order reference: {order_id}. The customer requests delivery by "
            f"{requested_date}."
        )
        task_instructions = (
            "Respond as the customer in this email conversation. Answer questions using "
            "only the known information. Do not invent delivery commitments, refunds, "
            "payments, or financial approvals. If the employee asks for an unsupported "
            "commitment, ask them to confirm it with the responsible team."
        )
        reason = "Follow up with the company about an earlier requested delivery date."
    else:
        raise ValueError("customer actor request_type is not supported by this τ task adapter")

    task_id = str(uuid5(NAMESPACE_URL, f"tac-tau2-customer:{run_id}:{generation}:{scenario_id}"))
    history = [
        {"role": "user", "content": initial_message["body"]}
    ] if initial_message else []
    # This is the upstream Task contract, not a second task/evaluation format.
    return {
        "id": task_id,
        "user_scenario": {
            "persona": "A concise business customer communicating by email.",
            "instructions": {
                "domain": "company financial-support customer correspondence",
                "reason_for_call": reason,
                "known_info": known_info,
                "unknown_info": "Do not infer facts beyond the supplied known information.",
                "task_instructions": task_instructions,
            },
        },
        "initial_state": {"message_history": history},
        "evaluation_criteria": {
            "actions": [
                {
                    "action_id": "inspect_customer_sales_order",
                    "name": "get_sales_order",
                    "arguments": {"entity_id": order_id},
                    "compare_args": ["entity_id"],
                },
                {
                    "action_id": "contact_customer",
                    "name": "send_message",
                    "arguments": {"to_actor_id": "customer"},
                    "compare_args": ["to_actor_id"],
                },
            ],
            "communicate_info": ["serial number"] if request_type == "damage" else ["delivery date"],
            "reward_basis": ["ACTION", "COMMUNICATE"],
        },
        "user_tools": [],
    }


def add_initial_customer_message(task: dict[str, Any], body: str) -> dict[str, Any]:
    """Add a scenario email to the native Task before the first user turn."""
    updated = dict(task)
    initial_state = dict(updated.get("initial_state") or {})
    history = list(initial_state.get("message_history") or [])
    history.append({"role": "user", "content": body})
    initial_state["message_history"] = history
    updated["initial_state"] = initial_state
    return updated

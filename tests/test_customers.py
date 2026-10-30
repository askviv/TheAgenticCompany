from servers.simulation.customers import add_initial_customer_message, build_customer_task


def test_damage_customer_adapter_builds_native_tau_task_with_private_facts_and_initial_mail():
    task = build_customer_task(
        run_id="run-17",
        generation=2,
        scenario_id="E1",
        private_state={
            "request_type": "damage",
            "order_id": "SAL-ORD-17",
            "damage_description": "a cracked housing",
            "serial_number": None,
        },
        initial_message={"body": "The order arrived damaged; I do not have serial numbers."},
    )

    assert task["id"]
    assert task["user_scenario"]["instructions"]["known_info"] == (
        "Customer order reference: SAL-ORD-17. Complaint: a cracked housing. "
        "The customer has not provided serial numbers."
    )
    assert task["initial_state"]["message_history"] == [
        {"role": "user", "content": "The order arrived damaged; I do not have serial numbers."}
    ]
    assert task["user_tools"] == []
    # The adapter creates upstream input data only; no local response text or
    # financial action is synthesized from private facts.
    assert "body" not in task
    assert "financial approvals" in task["user_scenario"]["instructions"]["task_instructions"].lower()


def test_s01_delivery_task_is_grounded_in_the_private_customer_date():
    task = build_customer_task(
        run_id="run-17",
        generation=0,
        scenario_id="S01",
        private_state={
            "request_type": "delivery_change",
            "order_id": "SAL-ORD-17",
            "requested_earlier_delivery_date": "2026-10-18",
        },
    )

    instructions = task["user_scenario"]["instructions"]
    assert instructions["reason_for_call"]
    assert "2026-10-18" in instructions["known_info"]
    assert "damage" not in instructions["known_info"].lower()


def test_later_customer_notice_is_added_to_native_task_history_once_by_caller():
    task = build_customer_task(
        run_id="run-17", generation=0, scenario_id="S01",
        private_state={
            "request_type": "delivery_change", "order_id": "SAL-ORD-17",
            "requested_earlier_delivery_date": "2026-10-18",
        },
    )
    updated = add_initial_customer_message(task, "Please deliver by 2026-10-18.")

    assert task["initial_state"]["message_history"] == []
    assert updated["initial_state"]["message_history"] == [
        {"role": "user", "content": "Please deliver by 2026-10-18."}
    ]

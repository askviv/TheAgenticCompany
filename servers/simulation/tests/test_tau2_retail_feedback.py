from __future__ import annotations

from servers.simulation.tau2_retail import UPSTREAM_COMMIT
from servers.simulation.feedback_contracts import CompanyEvidence, RecordMapping
from servers.simulation.tau2_retail_feedback import (
    TauRetailFeedbackAdapter,
    _mapped_fields,
    apply_native,
    translate,
)


def _mapping(**changes):
    result = {
        "mapping_id": "mapping-1",
        "company_id": "company",
        "benchmark": "tau_retail",
        "upstream_commit": UPSTREAM_COMMIT,
        "source_record_id": "#W-1",
        "source_entity_type": "order",
        "erp_doctype": "Sales Order",
        "erp_name": "SO-0001",
        "experiment_id": "run-1",
        "source_time": None,
        "company_time": "2026-10-30T12:00:00Z",
        "native_context": {
            "native_action": "cancel_pending_order",
            "events": ["on_cancel"],
            "native_ids": {"order_id": "#W-1"},
            "document_fields": {
                "before_state": "before_document.custom_customer_service_state",
                "docstatus": "docstatus",
                "current_state": "custom_customer_service_state",
                "reason": "custom_customer_service_cancel_reason",
            },
            "source_snapshot": {"order_id": "#W-1", "user_id": "user-1", "status": "pending", "payment_history": [
                {"transaction_type": "payment", "amount": 12.5, "payment_method_id": "card-1"},
            ]},
        },
        "message_ids": [],
    }
    result.update(changes)
    return result


def _evidence(**changes):
    result = {
        "evidence_id": "evidence-1",
        "company_id": "company",
        "erp_doctype": "Sales Order",
        "erp_name": "SO-0001",
        "event": "on_cancel",
        "actor": "operator@example.test",
        "occurred_at": "2026-10-30T12:00:00Z",
        "company_time": "2026-10-30T12:00:00Z",
        "before_document": {"custom_customer_service_state": "Pending"},
        "document": {
            "docstatus": 2,
            "custom_customer_service_state": "Cancelled",
            "custom_customer_service_cancel_reason": "no longer needed",
        },
        "native_observation": {"order": {"order_id": "#W-1", "user_id": "user-1", "status": "cancelled", "cancel_reason": "no longer needed", "payment_history": [
            {"transaction_type": "payment", "amount": 12.5, "payment_method_id": "card-1"},
            {"transaction_type": "refund", "amount": 12.5, "payment_method_id": "card-1"},
        ]}, "user": {"user_id": "user-1", "payment_methods": {"card-1": {"source": "credit_card", "id": "card-1", "brand": "visa", "last_four": "1111"}}}, "native_evidence": [
            {"doctype": "Sales Order", "name": "SO-0001", "docstatus": 2, "modified": "2026-10-30 12:00:00"},
            {"doctype": "Payment Entry", "name": "PE-0001", "docstatus": 1, "modified": "2026-10-30 12:01:00"},
            {"doctype": "Payment Entry", "name": "PE-0002", "docstatus": 1, "modified": "2026-10-30 12:02:00"},
        ]},
        "evidence_refs": ["erp://Sales Order/SO-0001/version/1"],
        "message_id": None,
        "in_reply_to": None,
        "experiment_id": "run-1",
    }
    result.update(changes)
    return result


def test_cancel_translation_requires_a_real_pending_order_cancel():
    translated = translate(_mapping(), _evidence())

    assert translated["status"] == "supported"
    assert translated["action"]["native_action"] == "cancel_pending_order"
    assert translated["action"]["arguments"] == {
        "order_id": "#W-1",
        "reason": "no longer needed",
    }
    assert translated["action"]["action_id"]
    assert translated["action"]["supporting_evidence"][0]["native_document_refs"] == _evidence()["native_observation"]["native_evidence"]
    assert "ERPNext Sales Order on_cancel" in translated["coverage"]


def test_typed_runtime_mapping_reads_top_level_predecessor_and_retains_native_refs():
    mapping = RecordMapping.model_validate(_mapping())
    raw_evidence = _evidence()
    raw_evidence.pop("native_observation")
    evidence = CompanyEvidence.model_validate(raw_evidence)
    mapped = _mapped_fields(
        mapping.native_context, evidence.model_dump(mode="json"),
        {"before_state", "docstatus", "current_state", "reason"},
    )
    assert mapped == {
        "order_id": "#W-1", "before_state": "Pending", "docstatus": 2,
        "current_state": "Cancelled", "reason": "no longer needed",
    }


def test_translation_without_company_time_is_inconclusive():
    result = translate(_mapping(), _evidence(company_time=None))
    assert result["status"] == "inconclusive"
    assert result["action"] is None


def test_cancel_draft_or_nonpending_order_is_inconclusive():
    evidence = _evidence(
        document={
            "docstatus": 2,
            "custom_customer_service_state": "Cancelled",
            "custom_customer_service_cancel_reason": "no longer needed",
        }
    )
    evidence["before_document"] = {"custom_customer_service_state": "Processed"}

    result = translate(_mapping(), evidence)
    assert result["status"] == "inconclusive"
    assert result["action"] is None


def test_paid_cancellation_without_completed_erp_refund_is_inconclusive():
    no_refund = _evidence(native_observation={
        "order": {"order_id": "#W-1", "user_id": "user-1", "status": "cancelled", "cancel_reason": "no longer needed", "payment_history": [
            {"transaction_type": "payment", "amount": 12.5, "payment_method_id": "card-1"},
        ]}, "user": {"user_id": "user-1", "payment_methods": {"card-1": {"source": "credit_card", "id": "card-1", "brand": "visa", "last_four": "1111"}}}, "native_evidence": [
            {"doctype": "Sales Order", "name": "SO-0001", "docstatus": 2, "modified": "2026-10-30 12:00:00"},
            {"doctype": "Payment Entry", "name": "PE-0001", "docstatus": 1, "modified": "2026-10-30 12:01:00"},
        ],
    })
    result = translate(_mapping(), no_refund)
    assert result["status"] == "inconclusive"
    assert "posted ERP refund history" in result["reason"]
    assert result["action"] is None


def test_paid_cancellation_requires_exact_posted_refund_amount_and_method():
    mismatch = _evidence(native_observation={
        "order": {"order_id": "#W-1", "user_id": "user-1", "status": "cancelled", "cancel_reason": "no longer needed", "payment_history": [
            {"transaction_type": "payment", "amount": 12.5, "payment_method_id": "card-1"},
            {"transaction_type": "refund", "amount": 12.5, "payment_method_id": "card-other"},
        ]}, "user": {"user_id": "user-1", "payment_methods": {"card-1": {"source": "credit_card", "id": "card-1", "brand": "visa", "last_four": "1111"}}}, "native_evidence": [
            {"doctype": "Sales Order", "name": "SO-0001", "docstatus": 2, "modified": "2026-10-30 12:00:00"},
            {"doctype": "Payment Entry", "name": "PE-0001", "docstatus": 1, "modified": "2026-10-30 12:01:00"},
            {"doctype": "Payment Entry", "name": "PE-0002", "docstatus": 1, "modified": "2026-10-30 12:02:00"},
        ],
    })
    result = translate(_mapping(), mismatch)
    assert result["status"] == "inconclusive"
    assert result["action"] is None


def test_cancellation_with_proven_zero_original_payment_needs_no_refund():
    mapping = _mapping(native_context={
        **_mapping()["native_context"],
        "source_snapshot": {"order_id": "#W-1", "user_id": "user-1", "status": "pending", "payment_history": []},
    })
    evidence = _evidence(native_observation={"order": {"order_id": "#W-1", "user_id": "user-1", "status": "cancelled", "cancel_reason": "no longer needed", "payment_history": []}, "user": {"user_id": "user-1", "payment_methods": {}}, "native_evidence": [
        {"doctype": "Sales Order", "name": "SO-0001", "docstatus": 2, "modified": "2026-10-30 12:00:00"},
    ]})
    result = translate(mapping, evidence)
    assert result["status"] == "supported"
    assert result["action"]["native_action"] == "cancel_pending_order"


def test_cancel_of_pending_item_modified_order_is_not_mapped():
    evidence = _evidence(
        document={
            "docstatus": 2,
            "custom_customer_service_state": "Cancelled",
            "custom_customer_service_cancel_reason": "no longer needed",
        }
    )
    evidence["before_document"] = {"custom_customer_service_state": "Pending (Item Modified)"}

    result = translate(_mapping(), evidence)
    assert result["status"] == "inconclusive"
    assert result["action"] is None


def test_return_issue_request_translates_to_native_request_without_refund():
    mapping = _mapping(
        erp_doctype="Issue",
        erp_name="ISS-0001",
        source_record_id="#W-1",
        source_entity_type="order",
        native_context={
            "task_id": "synthetic-task",
            "native_action": "return_delivered_order_items",
            "events": ["on_update"],
            "native_ids": {"order_id": "#W-1", "sales_order_name": "SO-0001"},
            "document_fields": {},
            "source_snapshot": {
                "order_id": "#W-1", "user_id": "user-1", "status": "delivered",
                "payment_history": [{"transaction_type": "payment", "amount": 10.0,
                                     "payment_method_id": "card-1"}],
            },
        },
    )
    issue_fields = {
        "status": "Open", "custom_customer_service_type": "Return",
        "custom_customer_service_order": "SO-0001",
        "custom_customer_service_item_ids": '["item-1"]',
        "custom_customer_service_payment_method": "MODE-CARD",
    }
    issue = _evidence(
        erp_doctype="Issue",
        erp_name="ISS-0001",
        event="on_update",
        document=issue_fields,
        native_observation={
            "order": {"order_id": "#W-1", "user_id": "user-1", "status": "return requested",
                      "return_items": ["item-1"], "return_payment_method_id": "card-1",
                      "payment_history": [{"transaction_type": "payment", "amount": 10.0,
                                           "payment_method_id": "card-1"}]},
            "user": {"user_id": "user-1", "payment_methods": {"card-1": {"source": "credit_card"}}},
            "service_issue": {"name": "ISS-0001", "status": "Open", "native_ref": {
                "doctype": "Issue", "name": "ISS-0001", "docstatus": 0,
                "modified": "2026-10-30 12:00:00"},
                **{key: value for key, value in issue_fields.items()},
                "custom_customer_service_item_ids": ["item-1"],
                "payment_method_id": "card-1"},
            "native_evidence": [{"doctype": "Sales Order", "name": "SO-0001",
                                 "docstatus": 1, "modified": "2026-10-30 12:00:00"}],
        },
    )
    result = translate(mapping, issue)
    assert result["status"] == "supported"
    assert result["action"]["arguments"] == {
        "order_id": "#W-1", "item_ids": ["item-1"], "payment_method_id": "card-1"
    }
    assert result["action"]["supporting_evidence"][0]["native_document_refs"][-1]["doctype"] == "Issue"


def test_mapping_identity_and_shared_record_conflict_are_not_guessed():
    mismatch = translate(_mapping(erp_name="SO-OTHER"), _evidence())
    shared = translate(
        _mapping(
            native_context={
                **_mapping()["native_context"],
                "shared_record_ambiguous": True,
            }
        ),
        _evidence(),
    )
    wrong_run = translate(_mapping(), _evidence(experiment_id="run-2"))

    assert mismatch["status"] == "unmapped"
    assert shared["status"] == "inconclusive"
    assert wrong_run["status"] == "inconclusive"


def test_native_method_selection_without_posted_financial_history_is_inconclusive():
    mapping = _mapping(
        native_context={
            "native_action": "modify_pending_order_payment",
            "events": ["on_update_after_submit"],
            "native_ids": {"order_id": "#W-1", "payment_method_id": "card-new"},
            "document_fields": {},
        }
    )
    evidence = _evidence(event="on_update_after_submit")
    result = translate(mapping, evidence)
    assert result["status"] == "inconclusive"
    assert "posted payment history" in result["reason"]


def test_pending_order_address_requires_native_submitted_pending_state():
    mapping = _mapping(
        native_context={
            "native_action": "modify_pending_order_address",
            "events": ["on_update_after_submit"],
            "native_ids": {"order_id": "#W-1"},
            "document_fields": {
                "address1": "shipping_address.address_line1",
                "address2": "shipping_address.address_line2",
                "city": "shipping_address.city",
                "state": "shipping_address.state",
                "country": "shipping_address.country",
                "zip": "shipping_address.pincode",
                "docstatus": "docstatus",
                "before_state": "before.custom_customer_service_state",
                "current_state": "custom_customer_service_state",
            },
        }
    )
    evidence = _evidence(
        event="on_update_after_submit",
        document={
            "docstatus": 1,
            "before": {"custom_customer_service_state": "Pending"},
            "custom_customer_service_state": "Pending",
            "shipping_address": {
                "address_line1": "123 Elm Street",
                "address_line2": "Suite 1",
                "city": "Austin",
                "state": "TX",
                "country": "United States",
                "pincode": "78712",
            },
        },
    )
    result = translate(mapping, evidence)
    assert result["status"] == "supported"
    assert result["action"]["arguments"] == {
        "order_id": "#W-1",
        "address1": "123 Elm Street",
        "address2": "Suite 1",
        "city": "Austin",
        "state": "TX",
        "country": "USA",
        "zip": "78712",
    }


def test_user_address_translation_requires_exact_customer_link():
    mapping = _mapping(
        source_record_id="user-1",
        source_entity_type="user",
        erp_doctype="Address",
        erp_name="ADDR-1",
        native_context={
            "native_action": "modify_user_address",
            "events": ["on_update"],
            "native_ids": {"user_id": "user-1", "customer_name": "CUSTOMER-1"},
            "document_fields": {
                "address1": "address_line1",
                "address2": "address_line2",
                "city": "city",
                "state": "state",
                "country": "country",
                "zip": "pincode",
            },
        },
    )
    evidence = _evidence(
        erp_doctype="Address",
        erp_name="ADDR-1",
        event="on_update",
        document={
            "address_line1": "101 Highway",
            "address_line2": "",
            "city": "New York",
            "state": "NY",
            "country": "United States",
            "pincode": "10001",
            "links": [{"link_doctype": "Customer", "link_name": "CUSTOMER-1"}],
        },
    )
    result = translate(mapping, evidence)
    assert result["status"] == "supported"
    assert result["action"]["arguments"] == {
        "user_id": "user-1",
        "address1": "101 Highway",
        "address2": "",
        "city": "New York",
        "state": "NY",
        "country": "USA",
        "zip": "10001",
    }

    wrong_link = translate(
        mapping,
        _evidence(
            erp_doctype="Address",
            erp_name="ADDR-1",
            event="on_update",
            document={
                **evidence["document"],
                "links": [
                    {"link_doctype": "Customer", "link_name": "OTHER"},
                ],
            },
        ),
    )
    assert wrong_link["status"] == "inconclusive"


def test_pending_item_change_preserves_duplicate_ids_and_requires_posted_payment_effect():
    from servers.simulation.tau2_retail import _ensure_upstream_data_root
    _ensure_upstream_data_root()
    from tau2.domains.retail.data_model import (
        CreditCard, GiftCard, Order, OrderItem, OrderPayment, Product, RetailDB,
        User, UserAddress, UserName, Variant,
    )
    from tau2.domains.retail.environment import get_environment

    address = UserAddress(address1="1 Test Way", address2="", city="Testville",
                          country="USA", state="TS", zip="00001")
    native_db = RetailDB(
        products={"product-1": Product(name="Shirt", product_id="product-1", variants={
            "item-1": Variant(item_id="item-1", options={"size": "S"}, available=True, price=10.0),
            "item-2": Variant(item_id="item-2", options={"size": "M"}, available=True, price=15.0),
        })},
        users={"user-1": User(
            user_id="user-1", name=UserName(first_name="Test", last_name="Customer"),
            address=address, email="customer@example.test",
            payment_methods={
                "card-1": CreditCard(source="credit_card", id="card-1", brand="visa", last_four="1111"),
                "gift-1": GiftCard(source="gift_card", id="gift-1", balance=20.0),
            }, orders=["#W-UPDATE"],
        )},
        orders={"#W-UPDATE": Order(
            order_id="#W-UPDATE", user_id="user-1", address=address,
            items=[
                OrderItem(name="Shirt", product_id="product-1", item_id="item-1", price=10.0, options={"size": "S"}),
                OrderItem(name="Shirt", product_id="product-1", item_id="item-1", price=10.0, options={"size": "S"}),
            ],
            status="pending", fulfillments=[],
            payment_history=[OrderPayment(transaction_type="payment", amount=20.0, payment_method_id="card-1")],
        )},
    )
    source_order = native_db.orders["#W-UPDATE"].model_dump(mode="json", by_alias=True)
    args = {
        "order_id": "#W-UPDATE", "item_ids": ["item-1", "item-1"],
        "new_item_ids": ["item-2", "item-2"], "payment_method_id": "gift-1",
    }
    expected_environment = get_environment(db=native_db.model_copy(deep=True))
    expected_environment.make_tool_call("modify_pending_order_items", requestor="assistant", **args)
    expected = expected_environment.tools.db.model_dump(mode="json", by_alias=True)
    references = [
        {"doctype": "Sales Order", "name": "SO-UPDATE", "docstatus": 1, "modified": "2026-10-30 12:00:00"},
        {"doctype": "Payment Entry", "name": "PE-ORIGINAL", "docstatus": 1, "modified": "2026-10-30 12:01:00"},
        {"doctype": "Payment Entry", "name": "PE-ITEM-CHANGE", "docstatus": 1, "modified": "2026-10-30 12:02:00"},
    ]
    mapping = _mapping(native_context={
        "native_action": "modify_pending_order_items", "events": ["on_update_after_submit"],
        "native_ids": {"order_id": "#W-UPDATE"}, "document_fields": {},
        "source_snapshot": source_order,
    }, source_record_id="#W-UPDATE", erp_name="SO-UPDATE")
    evidence = _evidence(
        event="on_update_after_submit", erp_name="SO-UPDATE",
        document={"docstatus": 1},
        native_observation={
            "order": expected["orders"]["#W-UPDATE"],
            "user": expected["users"]["user-1"],
            "native_evidence": references,
        },
    )

    translated = translate(mapping, evidence)
    assert translated["status"] == "supported"
    assert translated["action"]["arguments"] == args
    applied = apply_native(get_environment(db=native_db.model_copy(deep=True)), translated["action"])
    assert applied.accepted and applied.reconciled
    assert applied.outcome["items"][0]["item_id"] == "item-2"
    assert applied.outcome["items"][1]["item_id"] == "item-2"
    assert applied.outcome["payment_history"][-1] == {
        "transaction_type": "payment", "amount": 10.0, "payment_method_id": "gift-1",
    }


def test_pending_payment_method_change_requires_posted_payment_and_refund():
    from servers.simulation.tau2_retail import _ensure_upstream_data_root
    _ensure_upstream_data_root()
    from tau2.domains.retail.data_model import (
        CreditCard, GiftCard, Order, OrderPayment, RetailDB, User, UserAddress, UserName,
    )
    from tau2.domains.retail.environment import get_environment

    address = UserAddress(address1="1 Test Way", address2="", city="Testville",
                          country="USA", state="TS", zip="00001")
    native_db = RetailDB(
        products={},
        users={"user-1": User(
            user_id="user-1", name=UserName(first_name="Test", last_name="Customer"),
            address=address, email="customer@example.test",
            payment_methods={
                "card-old": CreditCard(source="credit_card", id="card-old", brand="visa", last_four="1111"),
                "gift-new": GiftCard(source="gift_card", id="gift-new", balance=20.0),
            }, orders=["#W-PAYMENT"],
        )},
        orders={"#W-PAYMENT": Order(
            order_id="#W-PAYMENT", user_id="user-1", address=address, items=[],
            status="pending", fulfillments=[],
            payment_history=[OrderPayment(transaction_type="payment", amount=12.5, payment_method_id="card-old")],
        )},
    )
    source_order = native_db.orders["#W-PAYMENT"].model_dump(mode="json", by_alias=True)
    args = {"order_id": "#W-PAYMENT", "payment_method_id": "gift-new"}
    expected_environment = get_environment(db=native_db.model_copy(deep=True))
    expected_environment.make_tool_call("modify_pending_order_payment", requestor="assistant", **args)
    expected = expected_environment.tools.db.model_dump(mode="json", by_alias=True)
    references = [
        {"doctype": "Sales Order", "name": "SO-PAYMENT", "docstatus": 1, "modified": "2026-10-30 12:00:00"},
        {"doctype": "Payment Entry", "name": "PE-ORIGINAL", "docstatus": 1, "modified": "2026-10-30 12:01:00"},
        {"doctype": "Payment Entry", "name": "PE-NEW-PAYMENT", "docstatus": 1, "modified": "2026-10-30 12:02:00"},
        {"doctype": "Payment Entry", "name": "PE-OLD-REFUND", "docstatus": 1, "modified": "2026-10-30 12:03:00"},
    ]
    mapping = _mapping(native_context={
        "native_action": "modify_pending_order_payment", "events": ["on_update_after_submit"],
        "native_ids": {"order_id": "#W-PAYMENT"}, "document_fields": {},
        "source_snapshot": source_order,
    }, source_record_id="#W-PAYMENT", erp_name="SO-PAYMENT")
    evidence = _evidence(
        event="on_update_after_submit", erp_name="SO-PAYMENT", document={"docstatus": 1},
        native_observation={
            "order": expected["orders"]["#W-PAYMENT"],
            "user": expected["users"]["user-1"], "native_evidence": references,
        },
    )
    translated = translate(mapping, evidence)
    assert translated["status"] == "supported"
    assert translated["action"]["arguments"] == args
    applied = apply_native(get_environment(db=native_db.model_copy(deep=True)), translated["action"])
    assert applied.accepted and applied.reconciled
    assert applied.outcome["payment_history"] == [
        {"transaction_type": "payment", "amount": 12.5, "payment_method_id": "card-old"},
        {"transaction_type": "payment", "amount": 12.5, "payment_method_id": "gift-new"},
        {"transaction_type": "refund", "amount": 12.5, "payment_method_id": "card-old"},
    ]

    unposted = {**evidence, "native_observation": {
        **evidence["native_observation"],
        "order": source_order,
        "native_evidence": references[:2],
    }}
    assert translate(mapping, unposted)["status"] == "inconclusive"


class _FakeRetailDB:
    def __init__(self):
        self.orders = {
            "#W-1": {
                "order_id": "#W-1",
                "user_id": "user-1",
                "status": "pending",
                "cancel_reason": None,
                "payment_history": [
                    {"transaction_type": "payment", "amount": 12.5, "payment_method_id": "card-1"}
                ],
            },
        }
        self.users = {"user-1": {"user_id": "user-1", "payment_methods": {
            "card-1": {"source": "credit_card", "id": "card-1", "brand": "visa", "last_four": "1111"}
        }}}

    def model_dump(self, **_kwargs):
        return {"orders": self.orders, "users": self.users}


class _FakeEnvironment:
    def __init__(self):
        self.tools = type("Tools", (), {"db": _FakeRetailDB()})()
        self.calls = []

    def make_tool_call(self, tool_name, *, requestor, **arguments):
        self.calls.append((tool_name, requestor, arguments))
        order = self.tools.db.orders[arguments["order_id"]]
        order["status"] = "cancelled"
        order["cancel_reason"] = arguments["reason"]
        order["payment_history"].append({
            "transaction_type": "refund", "amount": 12.5, "payment_method_id": "card-1"
        })
        return order


def test_apply_native_calls_upstream_retail_tool_and_reconciles_actual_db_state():
    environment = _FakeEnvironment()
    action = translate(_mapping(), _evidence())["action"]

    receipt = apply_native(environment, action)

    assert environment.calls == [
        (
            "cancel_pending_order",
            "assistant",
            {"order_id": "#W-1", "reason": "no longer needed"},
        )
    ]
    assert receipt.accepted and receipt.reconciled
    assert receipt.outcome["status"] == "cancelled"
    assert receipt.outcome["cancel_reason"] == "no longer needed"
    assert receipt.native_state_ref.startswith("tau-retail-db:")


def test_feedback_adapter_delegates_durable_action_and_original_evaluator_callbacks():
    seen = {}
    adapter = TauRetailFeedbackAdapter(
        apply_action=lambda action: {
            "action_id": action["action_id"],
            "accepted": True,
            "reconciled": True,
            "native_state_ref": "worker-state-1",
            "outcome": {"status": "cancelled"},
        },
        reconcile_action=lambda action: None,
        evaluate_native=lambda mappings, evidence: (
            seen.update(
                {
                    "mapping_count": len(mappings),
                    "evidence_count": len(evidence),
                }
            )
            or {"task_id": "0", "reward": 1.0}
        ),
    )
    translated = adapter.translate(_mapping(), _evidence())
    receipt = adapter.apply(translated.action)
    result = adapter.evaluate([_mapping()], [_evidence()])

    assert receipt.accepted and receipt.reconciled
    assert seen == {"mapping_count": 1, "evidence_count": 1}
    assert result["native_evaluation"] == {"task_id": "0", "reward": 1.0}
    assert (
        result["translations"][0]["action"]["native_action"] == "cancel_pending_order"
    )


def test_settlement_observation_is_reported_separately_from_original_task_score():
    settlement = {
        "coverage": "native_settlement_not_defined",
        "sales_invoices": [{
            "doctype": "Sales Invoice", "name": "SINV-RETURN-1", "docstatus": 1,
            "currency": "USD", "grand_total": -12.5,
        }],
        "payment_entries": [],
        "delivery_notes": [],
    }
    adapter = TauRetailFeedbackAdapter(
        apply_action=lambda _action: None,
        reconcile_action=lambda _action: None,
        evaluate_native=lambda _mappings, _evidence: {"task_id": "0", "reward": 0.0},
        enrich_evidence=lambda _mapping, evidence: {
            **evidence,
            "native_observation": {"service_issue": {"native_settlement": settlement}},
        },
    )
    mapping = _mapping()
    evidence = _evidence()
    original_observation = evidence["native_observation"].copy()
    result = adapter.evaluate([mapping], [evidence])

    assert result["native_evaluation"] == {"task_id": "0", "reward": 0.0}
    assert result["native_settlement_outcomes"] == [{
        "mapping_id": "mapping-1",
        "source_record_id": "#W-1",
        "native_outcome": settlement,
    }]
    assert "native_settlement_not_defined" in result["coverage"]
    assert evidence["native_observation"] == original_observation


def test_native_worker_checkpoint_and_receipt_commit_once_for_synthetic_retail_record(
    tmp_path, monkeypatch
):
    from servers.simulation import tau2_retail

    tau2_retail._ensure_upstream_data_root()
    from tau2.domains.retail.data_model import (
        CreditCard,
        Order,
        OrderPayment,
        RetailDB,
        User,
        UserAddress,
        UserName,
    )
    from tau2.domains.retail.environment import get_environment
    from servers.simulation.actor_worker_store import ActorTurnStore
    from servers.simulation.tau2_retail_feedback import (
        apply_persisted_native,
        initialize_native_state,
        reconcile_persisted_native,
    )

    native_db = RetailDB(
        products={},
        users={
            "user-synthetic": User(
                user_id="user-synthetic",
                name=UserName(first_name="Test", last_name="Customer"),
                address=UserAddress(
                    address1="1 Test Way",
                    address2="",
                    city="Testville",
                    country="USA",
                    state="TS",
                    zip="00001",
                ),
                email="synthetic@example.test",
                payment_methods={
                    "card-synthetic": CreditCard(
                        source="credit_card",
                        id="card-synthetic",
                        brand="visa",
                        last_four="0000",
                    )
                },
                orders=["#W-SYNTHETIC"],
            )
        },
        orders={
            "#W-SYNTHETIC": Order(
                order_id="#W-SYNTHETIC",
                user_id="user-synthetic",
                address=UserAddress(
                    address1="1 Test Way",
                    address2="",
                    city="Testville",
                    country="USA",
                    state="TS",
                    zip="00001",
                ),
                items=[],
                status="pending",
                fulfillments=[],
                payment_history=[
                    OrderPayment(
                        transaction_type="payment",
                        amount=12.5,
                        payment_method_id="card-synthetic",
                    )
                ],
            )
        },
    )
    monkeypatch.setattr(
        tau2_retail,
        "task_manifest",
        lambda task_id: {
            "task_id": task_id,
            "original_db": {
                "orders": {"#W-SYNTHETIC": {}},
                "users": {},
                "products": {},
            },
        },
    )
    monkeypatch.setattr(
        tau2_retail,
        "load_observed_db",
        lambda _observation: native_db.model_copy(deep=True),
    )
    store = ActorTurnStore(tmp_path / "tau-worker.sqlite")
    assert (
        initialize_native_state(store, "synthetic-task", "synthetic-experiment", {})[
            "status"
        ]
        == "initialized"
    )
    expected_environment = get_environment(db=native_db.model_copy(deep=True))
    expected_environment.make_tool_call(
        "cancel_pending_order",
        requestor="assistant",
        order_id="#W-SYNTHETIC",
        reason="ordered by mistake",
    )
    expected_state = expected_environment.tools.db.model_dump(mode="json", by_alias=True)
    source_order = native_db.orders["#W-SYNTHETIC"].model_dump(mode="json", by_alias=True)
    action = {
        "action_id": "synthetic-action-1",
        "mapping_id": "synthetic-mapping",
        "evidence_id": "synthetic-evidence",
        "benchmark": "tau_retail",
        "native_action": "cancel_pending_order",
        "arguments": {
            "order_id": "#W-SYNTHETIC",
            "reason": "ordered by mistake",
        },
        "supporting_evidence": [{
            "kind": "tau_retail_current_erp_projection",
            "source_order": source_order,
            "order": expected_state["orders"]["#W-SYNTHETIC"],
            "user": {"user_id": "user-synthetic",
                     "payment_methods": expected_state["users"]["user-synthetic"]["payment_methods"]},
            "native_document_refs": [
                {"doctype": "Sales Order", "name": "SO-SYNTHETIC", "docstatus": 2,
                 "modified": "2026-10-30 12:00:00"},
                {"doctype": "Payment Entry", "name": "PE-ORIGINAL", "docstatus": 1,
                 "modified": "2026-10-30 12:01:00"},
                {"doctype": "Payment Entry", "name": "PE-REFUND", "docstatus": 1,
                 "modified": "2026-10-30 12:02:00"},
            ],
        }],
        "company_time": "2026-10-30T12:00:00Z",
    }

    receipt = apply_persisted_native(
        store, "synthetic-task", "synthetic-experiment", action
    )
    repeated = apply_persisted_native(
        store, "synthetic-task", "synthetic-experiment", action
    )
    reconciled = reconcile_persisted_native(
        store, "synthetic-task", "synthetic-experiment", action
    )
    retried_initialization = initialize_native_state(
        store, "synthetic-task", "synthetic-experiment", {}
    )

    assert receipt == repeated == reconciled
    assert receipt.accepted and receipt.reconciled
    assert retried_initialization["status"] == "already_initialized"
    assert retried_initialization["projection_matches_initial"] is True
    with store._connect() as connection:
        row = connection.execute(
            "SELECT database_json FROM tau_retail_feedback_states WHERE experiment_id=? AND task_id=?",
            ("synthetic-experiment", "synthetic-task"),
        ).fetchone()
        receipt_count = connection.execute(
            "SELECT COUNT(*) FROM tau_retail_feedback_receipts WHERE experiment_id=? AND task_id=?",
            ("synthetic-experiment", "synthetic-task"),
        ).fetchone()[0]
    persisted = RetailDB.model_validate_json(row["database_json"])
    assert persisted.orders["#W-SYNTHETIC"].status == "cancelled"
    assert (
        persisted.orders["#W-SYNTHETIC"].payment_history[-1].transaction_type
        == "refund"
    )
    assert receipt_count == 1


def test_persisted_gift_card_cancel_requires_erp_projected_balance_and_commits_once(
    tmp_path, monkeypatch
):
    from servers.simulation import tau2_retail
    tau2_retail._ensure_upstream_data_root()
    from tau2.domains.retail.data_model import (
        GiftCard, Order, OrderPayment, RetailDB, User, UserAddress, UserName,
    )
    from tau2.domains.retail.environment import get_environment
    from servers.simulation.actor_worker_store import ActorTurnStore
    from servers.simulation.tau2_retail_feedback import (
        apply_persisted_native, initialize_native_state,
    )

    address = UserAddress(address1="1 Test Way", address2="", city="Testville",
                          country="USA", state="TS", zip="00001")
    native_db = RetailDB(
        products={},
        users={"user-gift": User(
            user_id="user-gift", name=UserName(first_name="Test", last_name="Gift"),
            address=address, email="gift@example.test",
            payment_methods={"gift-1": GiftCard(source="gift_card", id="gift-1", balance=1.0)},
            orders=["#W-GIFT"],
        )},
        orders={"#W-GIFT": Order(
            order_id="#W-GIFT", user_id="user-gift", address=address, items=[],
            status="pending", fulfillments=[],
            payment_history=[OrderPayment(transaction_type="payment", amount=6.0,
                                          payment_method_id="gift-1")],
        )},
    )
    monkeypatch.setattr(tau2_retail, "task_manifest", lambda task_id: {
        "task_id": task_id,
        "original_db": {"orders": {"#W-GIFT": {}}, "users": {}, "products": {}},
    })
    monkeypatch.setattr(tau2_retail, "load_observed_db",
                        lambda _observation: native_db.model_copy(deep=True))
    store = ActorTurnStore(tmp_path / "tau-gift-worker.sqlite")
    initialize_native_state(store, "gift-task", "gift-run", {})

    expected_environment = get_environment(db=native_db.model_copy(deep=True))
    expected_environment.make_tool_call(
        "cancel_pending_order", requestor="assistant", order_id="#W-GIFT",
        reason="no longer needed",
    )
    expected_state = expected_environment.tools.db.model_dump(mode="json", by_alias=True)
    source_order = native_db.orders["#W-GIFT"].model_dump(mode="json", by_alias=True)
    refs = [
        {"doctype": "Sales Order", "name": "SO-GIFT", "docstatus": 2,
         "modified": "2026-10-30 12:00:00"},
        {"doctype": "Payment Entry", "name": "PE-GIFT-PAY", "docstatus": 1,
         "modified": "2026-10-30 12:01:00"},
        {"doctype": "Payment Entry", "name": "PE-GIFT-REFUND", "docstatus": 1,
         "modified": "2026-10-30 12:02:00"},
    ]

    def make_action(action_id: str, user_projection: dict[str, Any]):
        return {
            "action_id": action_id, "mapping_id": "gift-map", "evidence_id": "gift-evidence",
            "benchmark": "tau_retail", "native_action": "cancel_pending_order",
            "arguments": {"order_id": "#W-GIFT", "reason": "no longer needed"},
            "supporting_evidence": [{
                "kind": "tau_retail_current_erp_projection", "source_order": source_order,
                "order": expected_state["orders"]["#W-GIFT"],
                "user": user_projection, "native_document_refs": refs,
            }],
            "company_time": "2026-10-30T12:00:00Z",
        }

    initial_user = native_db.users["user-gift"].model_dump(mode="json", by_alias=True)
    bad_projection = {"user_id": "user-gift", "payment_methods": initial_user["payment_methods"]}
    rejected = apply_persisted_native(
        store, "gift-task", "gift-run", make_action("gift-balance-unchanged", bad_projection),
    )
    assert rejected.accepted is False and rejected.reconciled is False
    with store._connect() as connection:
        state_row = connection.execute(
            "SELECT database_json FROM tau_retail_feedback_states WHERE experiment_id=? AND task_id=?",
            ("gift-run", "gift-task"),
        ).fetchone()
    assert RetailDB.model_validate_json(state_row["database_json"]) == native_db

    expected_user = {
        "user_id": "user-gift",
        "payment_methods": expected_state["users"]["user-gift"]["payment_methods"],
    }
    valid_action = make_action("gift-balance-reconciled", expected_user)
    applied = apply_persisted_native(store, "gift-task", "gift-run", valid_action)
    repeated = apply_persisted_native(store, "gift-task", "gift-run", valid_action)
    assert applied.accepted and applied.reconciled and applied == repeated
    with store._connect() as connection:
        state_row = connection.execute(
            "SELECT database_json FROM tau_retail_feedback_states WHERE experiment_id=? AND task_id=?",
            ("gift-run", "gift-task"),
        ).fetchone()
        receipt_count = connection.execute(
            "SELECT COUNT(*) FROM tau_retail_feedback_receipts WHERE experiment_id=? AND task_id=?",
            ("gift-run", "gift-task"),
        ).fetchone()[0]
    persisted = RetailDB.model_validate_json(state_row["database_json"])
    assert persisted.users["user-gift"].payment_methods["gift-1"].balance == 7.0
    assert receipt_count == 2


def test_persisted_return_and_exchange_requests_do_not_move_money(tmp_path, monkeypatch):
    from servers.simulation import tau2_retail
    tau2_retail._ensure_upstream_data_root()
    from tau2.domains.retail.data_model import (
        GiftCard, Order, OrderItem, OrderPayment, Product, RetailDB, User,
        UserAddress, UserName, Variant,
    )
    from tau2.domains.retail.environment import get_environment
    from servers.simulation.actor_worker_store import ActorTurnStore
    from servers.simulation.tau2_retail_feedback import (
        apply_persisted_native, initialize_native_state,
    )

    address = UserAddress(address1="1 Test Way", address2="", city="Testville",
                          country="USA", state="TS", zip="00001")
    native_db = RetailDB(
        products={"product-1": Product(
            name="Test Shirt", product_id="product-1", variants={
                "item-1": Variant(item_id="item-1", options={"size": "S"}, available=True, price=10.0),
                "item-2": Variant(item_id="item-2", options={"size": "M"}, available=True, price=15.0),
            },
        )},
        users={"user-service": User(
            user_id="user-service", name=UserName(first_name="Test", last_name="Service"),
            address=address, email="service@example.test",
            payment_methods={"gift-service": GiftCard(source="gift_card", id="gift-service", balance=20.0)},
            orders=["#W-SERVICE"],
        )},
        orders={"#W-SERVICE": Order(
            order_id="#W-SERVICE", user_id="user-service", address=address,
            items=[OrderItem(name="Test Shirt", product_id="product-1", item_id="item-1",
                             price=10.0, options={"size": "S"})],
            status="delivered", fulfillments=[],
            payment_history=[OrderPayment(transaction_type="payment", amount=10.0,
                                          payment_method_id="gift-service")],
        )},
    )
    monkeypatch.setattr(tau2_retail, "task_manifest", lambda task_id: {
        "task_id": task_id,
        "original_db": {"orders": {"#W-SERVICE": {}}, "users": {}, "products": {}},
    })
    monkeypatch.setattr(tau2_retail, "load_observed_db",
                        lambda _observation: native_db.model_copy(deep=True))

    for native_action, arguments, issue_type, issue_name in (
        ("return_delivered_order_items", {
            "order_id": "#W-SERVICE", "item_ids": ["item-1"],
            "payment_method_id": "gift-service",
        }, "Return", "ISS-RETURN"),
        ("exchange_delivered_order_items", {
            "order_id": "#W-SERVICE", "item_ids": ["item-1"],
            "new_item_ids": ["item-2"], "payment_method_id": "gift-service",
        }, "Exchange", "ISS-EXCHANGE"),
    ):
        environment = get_environment(db=native_db.model_copy(deep=True))
        environment.make_tool_call(native_action, requestor="assistant", **arguments)
        projected_db = environment.tools.db.model_dump(mode="json", by_alias=True)
        source_order = native_db.orders["#W-SERVICE"].model_dump(mode="json", by_alias=True)
        source_user = projected_db["users"]["user-service"]
        store = ActorTurnStore(tmp_path / f"{native_action}.sqlite")
        task_id = f"task-{native_action}"
        run_id = f"run-{native_action}"
        initialize_native_state(store, task_id, run_id, {})
        action = {
            "action_id": f"action-{native_action}", "mapping_id": "service-map",
            "evidence_id": "service-evidence", "benchmark": "tau_retail",
            "native_action": native_action, "arguments": arguments,
            "supporting_evidence": [{
                "kind": "tau_retail_current_erp_projection",
                "source_order": source_order,
                "order": projected_db["orders"]["#W-SERVICE"],
                "user": {"user_id": "user-service", "payment_methods": source_user["payment_methods"]},
                "service_issue": {"name": issue_name, "status": "Open"},
                "native_document_refs": [
                    {"doctype": "Sales Order", "name": "SO-SERVICE", "docstatus": 1,
                     "modified": "2026-10-30 12:00:00"},
                    {"doctype": "Issue", "name": issue_name, "docstatus": 0,
                     "modified": "2026-10-30 12:01:00"},
                ],
            }],
            "company_time": "2026-10-30T12:00:00Z",
        }
        receipt = apply_persisted_native(store, task_id, run_id, action)
        repeated = apply_persisted_native(store, task_id, run_id, action)
        assert receipt.accepted and receipt.reconciled and receipt == repeated
        with store._connect() as connection:
            row = connection.execute(
                "SELECT database_json FROM tau_retail_feedback_states WHERE experiment_id=? AND task_id=?",
                (run_id, task_id),
            ).fetchone()
        persisted = RetailDB.model_validate_json(row["database_json"])
        assert persisted.orders["#W-SERVICE"].status == (
            "return requested" if issue_type == "Return" else "exchange requested"
        )
        assert persisted.orders["#W-SERVICE"].payment_history == native_db.orders["#W-SERVICE"].payment_history
        assert persisted.users["user-service"].payment_methods["gift-service"].balance == 20.0

from __future__ import annotations

from servers.simulation.finance_sources import NativeFinanceSourceAdapter

COFFEE_PROVIDER = "sakanaai-coffeebench-native-economy"
ECOMMERCE_PROVIDER = "qwenlm-ecommercebench-calendar"


def test_finance_source_bridge_sends_native_facts_and_returns_native_receipts():
    received = []

    def reconcile(records, binding, as_of):
        received.append((records, binding, as_of))
        return {"receipts": [{"source_id": records[0]["id"], "doctype": "Purchase Invoice", "name": "PINV-0001"}]}

    adapter = NativeFinanceSourceAdapter(reconcile, lambda company, as_of: [{"name": "PINV-0001"}])
    binding = {
        "company_id": "company",
        "erp_company": "The Agent Company",
        "company_business_ids": ["roaster_A"],
        "counterparty_mail_directory": {"farmer_A": {
            "actor_id": "coffee_farmer_A", "email": "coffee-farmer_A@example.com",
            "display_name": "CoffeeBench Farmer A", "kind": "counterparty",
        }},
    }
    native_record = {
        "id": "b" * 32,
        "source": COFFEE_PROVIDER,
        "kind": "invoice",
        "party": "farmer_A",
        "currency": "USD",
        "amount": "120.00",
        "source_ref": "deal-7",
        "occurred_at": "2026-10-30T00:00:00+00:00",
        "native_record_type": "invoice",
        "native_record_id": "inv-7",
        "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
        "native_record": {
            "invoice_id": "inv-7", "issuer_id": "farmer_A", "payer_id": "roaster_A",
            "amount": "120.00", "issue_day": 0, "due_day": 7, "reference": "deal-7",
            "credited_amount": "0.00", "late_fees_accrued": "0.00", "paid": False,
            "paid_day": None, "returned": False, "bad_debt": False,
            "upstream_source": "coffeebench/typings.py:Invoice",
        },
    }
    order_record = {
        "id": "c" * 32,
        "source": COFFEE_PROVIDER,
        "kind": "order",
        "currency": "USD",
        "occurred_at": "2026-10-29T12:00:00+00:00",
        "native_record_type": "deal",
        "native_record_id": "deal-8",
        "native_record": {
            "deal_id": "deal-8", "seller_id": "roaster_A", "buyer_id": "buyer-8",
            "item_id": "green_coffee_kg", "quantity": 2, "unit_price": "10.00",
            "delivery_at_virtual_minute": 18000,
        },
    }
    result = adapter.reconcile(
        [native_record, order_record, {"id": "other", "source": ECOMMERCE_PROVIDER}],
        binding, "2026-10-30T12:00:00+00:00"
    )
    assert received == [([native_record, order_record], binding, "2026-10-30T12:00:00+00:00")]
    assert result["receipts"][0]["name"] == "PINV-0001"
    assert adapter.read_due_obligations("The Agent Company", "2026-10-30") == [{"name": "PINV-0001"}]


def test_finance_source_bridge_preserves_pending_offer_facts_for_native_import():
    received = []
    offer = {
        "id": "offer-source-1",
        "source": COFFEE_PROVIDER,
        "kind": "offer",
        "party": "retailer_A",
        "currency": "USD",
        "amount": "49.00",
        "source_ref": "offer-1",
        "occurred_at": "2026-10-30T12:03:45+00:00",
        "native_record_type": "offer",
        "native_record_id": "offer-1",
        "upstream_commit": "9e2f3950b5812c8753de81ede9c68babc024b5c8",
        "native_record": {
            "offer_id": "offer-1", "listing_id": "listing-1",
            "buyer_id": "retailer_A", "seller_id": "roaster_A",
            "item_id": "roasted_coffee_kg", "quantity": 4,
            "unit_price": "12.25", "total_price": "49.00",
            "payment_terms_days": 30, "offer_status": "pending",
        },
    }

    adapter = NativeFinanceSourceAdapter(
        lambda records, _binding, _as_of: received.extend(records) or {"receipts": []},
        lambda *_args: [],
    )
    adapter.reconcile([offer], {
        "company_id": "company", "erp_company": "The Agent Company",
        "company_business_ids": ["roaster_A"],
        "counterparty_mail_directory": {"retailer_A": {
            "actor_id": "coffee_retailer_A", "email": "coffee-retailer_A@example.com",
            "display_name": "CoffeeBench Retailer A", "kind": "counterparty",
        }},
    }, "2026-10-30")

    assert received == [offer]


def test_finance_source_bridge_fails_if_coffee_party_has_no_mail_directory_contract():
    adapter = NativeFinanceSourceAdapter(lambda *_args: {"receipts": []}, lambda *_args: [])

    try:
        adapter.reconcile([{"id": "offer-1", "source": COFFEE_PROVIDER}], {
            "company_id": "company", "erp_company": "The Agent Company",
            "company_business_ids": ["roaster_A"],
        }, "2026-10-30")
    except ValueError as exc:
        assert "counterparty mail directory" in str(exc)
    else:
        raise AssertionError("Coffee financial imports require the configured counterparty directory")


def test_finance_source_bridge_rejects_unrecognized_provenance():
    adapter = NativeFinanceSourceAdapter(lambda *args: {}, lambda *args: [])
    try:
        adapter.reconcile([{"id": "x", "source": "unrecognized-provider"}], {
            "company_id": "company", "erp_company": "The Agent Company",
            "company_business_ids": ["roaster_A"],
        }, "2026-10-30")
    except ValueError as exc:
        assert "unknown provider identity" in str(exc)
    else:
        raise AssertionError("unknown provenance must be visible instead of silently filtered")

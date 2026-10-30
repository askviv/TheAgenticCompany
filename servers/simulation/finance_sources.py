"""Host bridge for native ERP finance source reconciliation.

The ERP source reconciler runs inside the private site-admin operation. This
adapter passes provider facts to that operation and returns only its document
receipts to the company runtime; public/activity adapters must redact receipts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

COFFEE_SOURCE = "sakanaai-coffeebench-native-economy"
ECOMMERCE_SOURCE = "qwenlm-ecommercebench-calendar"


class NativeFinanceSourceAdapter:
    """Bridge to the existing private site-admin transport, supplied by host."""

    def __init__(
        self,
        reconcile_operation: Callable[[list[dict[str, Any]], dict[str, Any], str | datetime], dict[str, Any]],
        due_obligations_operation: Callable[[str, str | datetime], list[dict[str, Any]]],
    ) -> None:
        self._reconcile_operation = reconcile_operation
        self._due_obligations_operation = due_obligations_operation

    def reconcile(
        self,
        source_records: list[dict[str, Any]],
        company_binding: dict[str, Any],
        as_of: str | datetime,
    ) -> dict[str, Any]:
        """Import actual native source records and preserve receipt identities."""
        if company_binding.get("company_id") != "company":
            raise ValueError("Finance source reconciliation requires the active company binding")
        if not isinstance(company_binding.get("erp_company"), str) or not company_binding["erp_company"]:
            raise ValueError("Native ERP company binding is required")
        if not isinstance(company_binding.get("company_business_ids"), list) or "roaster_A" not in company_binding["company_business_ids"]:
            raise ValueError("CoffeeBench company business role mapping is required")
        unknown = {record.get("source") for record in source_records} - {COFFEE_SOURCE, ECOMMERCE_SOURCE}
        if unknown:
            raise ValueError("Finance source batch contains an unknown provider identity")
        records = [record for record in source_records if record.get("source") == COFFEE_SOURCE]
        if records:
            directory = company_binding.get("counterparty_mail_directory")
            if not isinstance(directory, dict) or not directory:
                raise ValueError("CoffeeBench source reconciliation requires the configured counterparty mail directory")
        return self._reconcile_operation(records, company_binding, as_of)

    def read_due_obligations(self, erp_company: str, as_of: str | datetime) -> list[dict[str, Any]]:
        """Read due native invoices without manufacturing them from stimuli."""
        if not erp_company:
            raise ValueError("Native ERP company binding is required")
        return self._due_obligations_operation(erp_company, as_of)


__all__ = ["NativeFinanceSourceAdapter"]

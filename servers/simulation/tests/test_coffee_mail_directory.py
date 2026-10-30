import json
from pathlib import Path
import runpy

import pytest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]


def _profile():
    return json.loads(
        (ROOT / "deploy/single-host/company-identities.json").read_text(encoding="utf-8")
    )["company"]


def _directory(profile):
    module = runpy.run_path(
        str(ROOT / "deploy/enterprise-simulation/coordinator/bootstrap.py")
    )
    return module["coffee_mail_directory"](profile)


def test_coffee_mail_directory_uses_exact_managed_native_ids_and_unique_mailboxes():
    accounts, actors = _directory(_profile())

    assert set(accounts) == {
        "coffee-farmer_A", "coffee-farmer_B", "coffee-roaster_B",
        "coffee-retailer_A", "coffee-retailer_B",
    }
    assert len({actor["actor_id"] for actor in actors}) == len(actors)
    assert len({actor["email"].casefold() for actor in actors}) == len(actors)
    assert accounts["coffee-farmer_A"] == (
        "coffee-farmer_A@example.com", "CoffeeBench Farmer A"
    )
    assert all(actor["kind"] == "counterparty" for actor in actors)


def test_coffee_mail_directory_rejects_aliases_that_do_not_match_native_business_ids():
    profile = _profile()
    profile["background"]["coffee"]["counterparties"]["farmer_A"]["email"] = (
        "northwind-supplier@example.com"
    )

    with pytest.raises(ValueError, match="managed simulator directory"):
        _directory(profile)


def test_coffee_mail_directory_requires_the_profile_company_sender_to_be_managed():
    profile = _profile()
    profile["background"]["coffee"]["mail_sender_actor_id"] = "northwind-supplier"

    with pytest.raises(ValueError, match="existing company mailbox actor"):
        _directory(profile)


def test_delivered_coffee_email_links_only_to_exact_mapped_document_and_recipient():
    from servers.simulation.company_feedback_runtime import CompanyFeedbackRuntime

    mapping = SimpleNamespace(
        mapping_id="coffee-map-1", benchmark="coffeebench",
        erp_doctype="Purchase Invoice", erp_name="PINV-7",
        native_context={"email_reply": {"allowed_recipient_actor_ids": ["coffee_farmer_A"]}},
    )

    class Pipeline:
        def __init__(self):
            self.linked = []

        def mappings(self):
            return [mapping]

        def link_message(self, mapping_id, message_id):
            self.linked.append((mapping_id, message_id))

    runtime = object.__new__(CompanyFeedbackRuntime)
    runtime.pipeline = Pipeline()
    message = {
        "related_document": {"doctype": "Purchase Invoice", "name": "PINV-7"},
        "to_actor_id": "coffee_farmer_A", "message_id": "<company-7@example.test>",
    }

    runtime._link_coffee_outbox_message(message)

    assert runtime.pipeline.linked == [("coffee-map-1", "<company-7@example.test>")]
    with pytest.raises(ValueError, match="recipient does not match"):
        runtime._link_coffee_outbox_message({**message, "to_actor_id": "coffee_farmer_B"})


def test_company_conversation_allowlist_includes_each_profile_employee_and_default_sender():
    from servers.simulation.company_feedback_runtime import _coffee_company_mail_actor_ids

    profile = json.loads(
        (ROOT / "deploy/single-host/company-identities.json").read_text(encoding="utf-8")
    )
    employees = profile["employees"]
    coffee = profile["company"]["background"]["coffee"]
    actors = [
        {"actor_id": row["id"], "email": row["email"], "kind": "employee"}
        for row in employees
    ]
    actors.append({"actor_id": "requester", "email": "company-mail@example.com", "kind": "employee"})
    actors.extend(
        {"actor_id": item["actor_id"], "email": item["email"], "kind": "counterparty"}
        for item in coffee["counterparties"].values()
    )
    actors.append({"actor_id": "viv", "email": "viv@example.test", "kind": "viv"})

    allowed = _coffee_company_mail_actor_ids(profile, {"actors": actors})

    assert set(allowed) == {row["id"] for row in employees} | {"requester"}


def test_actual_bootstrap_viv_identity_can_join_company_conversations():
    from servers.simulation.company_feedback_runtime import _coffee_company_mail_actor_ids

    profile = json.loads((ROOT / "deploy/single-host/company-identities.json").read_text())
    bootstrap = runpy.run_path(str(ROOT / "deploy/enterprise-simulation/coordinator/bootstrap.py"))
    actors = [*bootstrap["ACTORS"], *bootstrap["managed_employee_actors"](profile)]
    allowed = set(_coffee_company_mail_actor_ids(profile, {"actors": actors}))
    employee_emails = {employee["email"].casefold() for employee in profile["employees"]}
    expected_employees = {actor["actor_id"] for actor in actors
                          if actor["email"].casefold() in employee_emails}
    assert len(expected_employees) == 17
    assert allowed == expected_employees | {"viv"}

    # A different external address is not the configured company assistant.
    profile["company"]["assistantEmail"] = "another-agent@example.test"
    assert "viv" not in _coffee_company_mail_actor_ids(profile, {"actors": actors})

"""Wire native business evidence to private benchmark workers; no Viv tools."""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import traceback
import fcntl
import os
import re
from .background_runtime import BackgroundWorkerClient
from .actor_client import ActorTurnClient
from .coffee_feedback import CoffeeFeedbackAdapter
from .company_feedback import CompanyFeedback, identity
from .ecommerce_feedback import EcommerceNativeActionCheckpoint
from .feedback_contracts import CompanyEvidence, RecordMapping, TranslatedAction
from .evidence_observer import native_document_evidence
from .erpnext import ERPNextRetailAdmin, ERPNextSiteAdmin
from .tau2_retail_feedback import TauRetailFeedbackAdapter
from .integration_contracts import CompanySourceEvent


LOG = logging.getLogger(__name__)


def signing_secret(principals: dict) -> str:
    token = next(
        (
            token
            for token, principal in principals.items()
            if principal["role"] == "controller"
        ),
        None,
    )
    if token is None:
        raise ValueError(
            "Company evidence signing requires the configured controller principal"
        )
    return hmac.new(
        token.encode(), b"company-evidence-webhook-v1", hashlib.sha256
    ).hexdigest()


def _coffee_company_mail_actor_ids(profile: dict, mail_config: dict) -> list[str]:
    company = profile.get("company")
    coffee = company.get("background", {}).get("coffee") if isinstance(company, dict) else None
    if not isinstance(coffee, dict):
        raise ValueError("CoffeeBench company mail profile is missing")
    profile_emails = {
        row["email"].casefold()
        for row in profile.get("employees", [])
        if isinstance(row, dict) and isinstance(row.get("email"), str)
    }
    actors = mail_config.get("actors", [])
    if not isinstance(actors, list):
        raise ValueError("Mail actor directory is malformed")
    company_actor_kinds = {"employee", "warehouse", "logistics", "approver"}
    external_kinds = {"counterparty", "supplier", "customer", "viv"}
    allowed = [
        actor["actor_id"]
        for actor in actors
        if isinstance(actor, dict)
        and isinstance(actor.get("actor_id"), str)
        and isinstance(actor.get("email"), str)
        and actor["email"].casefold() in profile_emails
        and actor.get("actor_id") != "viv"
        and actor.get("kind") not in external_kinds
    ]
    actors_by_id = {
        actor.get("actor_id"): actor for actor in actors if isinstance(actor, dict)
    }
    # Viv may participate in an exact registered Coffee conversation when
    # the explicit company profile mailbox maps to a configured Viv actor.
    # Keep the actor ID as `viv`; Mailu authentication is still required by
    # each observer before a native send_message action is queued.
    viv_email = company.get("assistantEmail")
    viv_actor = actors_by_id.get("viv")
    if (isinstance(viv_email, str) and isinstance(viv_actor, dict)
            and viv_actor.get("kind") == "agent"
            and isinstance(viv_actor.get("email"), str)
            and viv_actor["email"].casefold() == viv_email.casefold()):
        allowed.append("viv")
    default_sender_id = coffee.get("mail_sender_actor_id")
    default_sender = actors_by_id.get(default_sender_id)
    if (
        not isinstance(default_sender, dict)
        or default_sender.get("kind") in external_kinds
        or default_sender_id == "viv"
        or not isinstance(default_sender.get("email"), str)
    ):
        raise ValueError("CoffeeBench default sender is not a configured company mailbox")
    if default_sender_id not in allowed:
        if default_sender.get("kind") not in company_actor_kinds:
            raise ValueError("CoffeeBench default sender is not a configured employee mailbox")
        allowed.append(default_sender_id)
    if not allowed:
        raise ValueError("CoffeeBench has no configured company employee mailboxes")
    return sorted(set(allowed))


class CompanyFeedbackRuntime:
    def __init__(
        self, config, store, principals, *, native=None, retail_admin=None, worker=None,
        provider_mail_observer=None,
    ):
        self.config, self.store = config, store
        self.secret = signing_secret(principals)
        self.native = native or ERPNextSiteAdmin(
            config.erp_compose, config.erp_credentials_root.parent
        )
        self.retail_admin = retail_admin or ERPNextRetailAdmin(
            config.erp_compose,
            config.erp_credentials_root.parent,
            operator_users=getattr(config, "retail_operator_users", []),
        )
        worker = worker or BackgroundWorkerClient(
            config.actor_workers.supplier_url,
            config.actor_workers.supplier_token,
            timeout_seconds=config.actor_workers.request_timeout_seconds,
        )
        coffee = CoffeeFeedbackAdapter(
            lambda action: worker.apply_company_action(action),
            lambda action: worker.reconcile_company_action(action),
            lambda actors: worker.evaluate_coffee_businesses(actors),
        )
        ecom = EcommerceNativeActionCheckpoint(
            store, config.state_root / "ecommerce-feedback"
        )
        self.actor_client = ActorTurnClient(config.actor_workers)
        tau = TauRetailFeedbackAdapter(
            self._apply_tau_retail_action,
            self._reconcile_tau_retail_action,
            self._evaluate_tau_retail,
            self._enrich_tau_retail_evidence,
        )
        self.pipeline = CompanyFeedback(
            store,
            {
                "coffeebench": coffee,
                "ecommercebench": ecom,
                "tau_retail": tau,
            },
        )
        self.lifecycle = {}
        self.error = None
        self.provider_mail_observer = provider_mail_observer
        self._mail_gateway = None
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS company_native_message_outbox(
                source_event_id TEXT PRIMARY KEY,
                mapping_id TEXT,
                message_id TEXT NOT NULL UNIQUE,
                request_hash TEXT NOT NULL,
                private_payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','submitting','uncertain','delivered')),
                submission_started INTEGER NOT NULL DEFAULT 0 CHECK(submission_started IN (0,1)),
                error_type TEXT,
                receipt_json TEXT,
                updated_at TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS company_coffee_mail_cursors(
                mapping_id TEXT NOT NULL,
                mailbox_actor_id TEXT NOT NULL,
                uidvalidity TEXT NOT NULL,
                after_uid INTEGER NOT NULL CHECK(after_uid >= 0),
                updated_at TEXT NOT NULL,
                PRIMARY KEY(mapping_id,mailbox_actor_id)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS company_coffee_thread_actions(
                action_id TEXT PRIMARY KEY,
                source_event_id TEXT NOT NULL,
                incoming_message_id TEXT NOT NULL,
                thread_key TEXT NOT NULL,
                native_actor_id TEXT NOT NULL,
                native_recipient_id TEXT NOT NULL,
                native_refs_json TEXT NOT NULL,
                company_mail_actor_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                action_json TEXT NOT NULL,
                mail_receipt_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','applying','uncertain','applied','rejected')),
                receipt_json TEXT,
                error_type TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(source_event_id,incoming_message_id,native_recipient_id)
            )""")

    def company_time(self):
        company = self.store.get_company()
        return datetime.fromisoformat(company["simulated_time"]) if company else None

    def accept(self, event):
        evidence = native_document_evidence(
            event,
            company_time=self.company_time(),
            erp_public_url=self.config.erp_public_url,
        )
        recorded = self.pipeline.capture(evidence)
        return [{"status": "recorded", "evidence_id": recorded.evidence_id}]

    def maintenance(self):
        try:
            self._maintenance()
        except Exception as error:
            self.error = type(error).__name__
            try:
                self._record_health({"status": "failed", "error_type": self.error})
            except Exception as report_error:
                error.add_note(
                    "Feedback health recording failed: " + type(report_error).__name__
                )
            raise
        self._record_health({"status": "ok"})

    def _record_health(self, result):
        result = {**result, "checked_at": datetime.now(timezone.utc).isoformat()}
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO company_feedback_runtime_reports VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET result_json=excluded.result_json",
                ("workflow_health", json.dumps(result, sort_keys=True)),
            )

    def _maintenance(self):
        failures = []
        # A provider/import failure must not block independent evidence or
        # already confirmed actions for another benchmark.
        for component, operation in (
            ("native_evidence", self._refresh_evidence),
            ("company_mail", self._observe_linked_mail),
            ("evidence_translation", self.pipeline.translate_captured),
            ("native_actions", self.pipeline.reconcile),
            (
                "ecommercebench",
                lambda: self._advance_ecommerce_lifecycle(self.company_time()),
            ),
        ):
            try:
                operation()
            except Exception as error:
                error.add_note("Company feedback component: " + component)
                failures.append(error)
                try:
                    with self.store.transaction() as db:
                        db.execute(
                            "INSERT INTO company_feedback_runtime_reports VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET result_json=excluded.result_json",
                            (
                                "component_" + component,
                                json.dumps(
                                    {
                                        "status": "failed",
                                        "error_type": type(error).__name__,
                                    }
                                ),
                            ),
                        )
                except Exception as report_error:
                    error.add_note(
                        "Feedback component status recording failed: "
                        + type(report_error).__name__
                    )
            else:
                with self.store.transaction() as db:
                    db.execute(
                        "DELETE FROM company_feedback_runtime_reports WHERE provider=?",
                        ("component_" + component,),
                    )
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise ExceptionGroup("Company feedback components failed", failures)
        self.error = None

    def _observe_linked_mail(self):
        self._link_issue_threads()
        self.observe_mail()

    def _refresh_evidence(self):
        with self.store.transaction() as db:
            prior = db.execute(
                "SELECT result_json FROM company_feedback_runtime_reports WHERE provider='native_evidence_cursor'"
            ).fetchone()
        cursor = json.loads(prior["result_json"]) if prior else None
        result = self.native.company_evidence_setup(
            f"http://{self.config.host}:{self.config.port}/company/evidence/native",
            self.secret,
            cursor,
        )
        existing = {m.mapping_id for m in self.pipeline.mappings()}
        now = self.company_time()
        if now is None:
            raise RuntimeError(
                "Existing shared company clock is unavailable for evidence mapping"
            )
        for record in result["records"]:
            source = record["source_system"]
            if source == "tau2:retail":
                # τ source rows are shared across tasks. Bind only the exact
                # task reference actions below, never dataset-owner records.
                continue
            benchmark = {
                "coffeebench": "coffeebench",
                "ecommercebench": "ecommercebench",
                "qwenlm-ecommercebench-native-economy": "ecommercebench",
            }.get(source)
            if benchmark is None:
                continue
            snapshot = record["source_snapshot"]
            commit = snapshot.get("upstream_commit")
            if not commit:
                raise ValueError(
                    "Mapped source record has no pinned benchmark revision"
                )
            mapping_id = identity(
                record["source_key"], record["doctype"], record["name"]
            )
            if mapping_id in existing:
                continue
            context = snapshot.get("feedback_context", {})
            if benchmark == "coffeebench":
                from .coffee_feedback import mapping_context_from_source

                profile = json.loads(self.config.company_profile_file.read_text())
                context = (
                    mapping_context_from_source(
                        snapshot,
                        record["document"],
                        profile["company"]["background"]["coffee"][
                            "company_business_ids"
                        ],
                    )
                    or {}
                )
                if context.get("email_reply"):
                    coffee = profile["company"]["background"]["coffee"]
                    configured_actors = {
                        actor["actor_id"]: actor for actor in self.config.mail["actors"]
                    }
                    sender_actor_ids = _coffee_company_mail_actor_ids(
                        profile, self.config.mail
                    )
                    counterparty_id = context["email_reply"].get("counterparty_id")
                    counterparty = coffee.get("counterparties", {}).get(counterparty_id)
                    if not isinstance(counterparty, dict):
                        raise ValueError(
                            "CoffeeBench source counterparty has no explicit company mail-directory entry"
                        )
                    recipient = configured_actors.get(counterparty.get("actor_id"))
                    if (
                        not recipient
                        or recipient.get("kind") != "counterparty"
                        or recipient.get("email", "").casefold()
                        != str(counterparty.get("email", "")).casefold()
                    ):
                        raise ValueError(
                            "CoffeeBench source counterparty does not resolve to its configured Mailu actor"
                        )
                    context["email_reply"]["allowed_mailbox_actor_ids"] = sender_actor_ids
                    context["email_reply"]["allowed_recipient_actor_ids"] = [
                        counterparty["actor_id"]
                    ]
            experiment = record["experiment_id"]
            mapping = RecordMapping(
                mapping_id=mapping_id,
                benchmark=benchmark,
                upstream_commit=commit,
                source_record_id=record["source_id"],
                source_entity_type=snapshot.get(
                    "native_record_type", record["doctype"]
                ),
                erp_doctype=record["doctype"],
                erp_name=record["name"],
                experiment_id=None
                if experiment in ("company", "background")
                else experiment,
                source_time=snapshot.get("occurred_at"),
                company_time=now,
                native_context=context,
            )
            self.pipeline.map_record(mapping, record["document"])
        self._register_tau_retail_mappings(result["records"], existing, now)
        from .feedback_contracts import NativeDocumentEvent

        for event in [*result.get("events", []), *result.get("issue_events", [])]:
            self.accept(NativeDocumentEvent.model_validate(event))
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO company_feedback_runtime_reports VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET result_json=excluded.result_json",
                ("native_evidence_cursor", json.dumps(result["cursor"])),
            )

    def _advance_ecommerce_lifecycle(self, company_time):
        provider = self.store.get_background_initialization_request("ecommercebench")
        if provider is None:
            self.lifecycle["ecommercebench"] = {"status": "not_initialized"}
            return
        if company_time is None:
            self.lifecycle["ecommercebench"] = {"status": "company_clock_unavailable"}
            return
        profile = json.loads(self.config.company_profile_file.read_text())
        bootstrap = self.pipeline.adapters["ecommercebench"].bootstrap_from_profile(
            profile,
            int(provider["seed"]),
            provider["started_at"],
        )
        advance = self.pipeline.adapters["ecommercebench"].advance_to(company_time)
        records = advance["source_records"]
        binding = {
            "company_id": "company",
            "erp_company": "The Agent Company",
            "money_unit": profile["company"]["background"]["ecommerce"]["money_unit"],
            "sales_order_date_semantics": profile["company"]["background"]["ecommerce"][
                "sales_order_date_semantics"
            ],
        }
        imported = self.native.reconcile_ecommerce_finance(
            records, binding, company_time.isoformat()
        )
        self.pipeline.adapters["ecommercebench"].acknowledge_source_receipts(imported["receipts"])
        self.lifecycle["ecommercebench"] = {
            "bootstrap": bootstrap,
            "advance": advance,
            "erp_reconciliation": imported,
        }
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO company_feedback_runtime_reports VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET result_json=excluded.result_json",
                (
                    "ecommercebench",
                    json.dumps(self.lifecycle["ecommercebench"], sort_keys=True),
                ),
            )

    def report(self, experiment_id=None):
        with self.store.transaction() as db:
            lifecycle = {
                row["provider"]: json.loads(row["result_json"])
                for row in db.execute(
                    "SELECT * FROM company_feedback_runtime_reports WHERE provider != 'native_evidence_cursor'"
                )
            }
        return {
            "company_feedback": self.pipeline.report(experiment_id),
            "lifecycle": lifecycle,
        }

    def _register_tau_retail_mappings(self, records, existing, now):
        """Bind only original τ WRITE references to their actual shared ERP rows."""
        mapped_docs = {
            (record["doctype"], record["name"]): record for record in records
        }
        for experiment in self.store.company_experiments():
            if experiment["scenario_id"] != "TAU_RETAIL":
                continue
            run = self.store.get_run(experiment["experiment_id"])
            prepared = run.get("manifest", {}).get("prepared_problem", {})
            manifest = prepared.get("snapshot") if isinstance(prepared, dict) else None
            task = manifest.get("task") if isinstance(manifest, dict) else None
            original_db = (
                manifest.get("original_db") if isinstance(manifest, dict) else None
            )
            receipt = (
                prepared.get("import_receipt") if isinstance(prepared, dict) else None
            )
            if (
                not isinstance(task, dict)
                or task.get("id") != prepared.get("task_id")
                or not isinstance(original_db, dict)
                or not isinstance(receipt, dict)
            ):
                continue
            native_orders = receipt.get("native_documents", {}).get("orders", {})
            if not isinstance(native_orders, dict):
                continue
            native_addresses = receipt.get("native_documents", {}).get("addresses", {})
            if not isinstance(native_addresses, dict):
                native_addresses = {}
            criteria = task.get("evaluation_criteria") or {}
            actions = criteria.get("actions") or []
            message_id = prepared.get("initial_email", {}).get("message_id")
            # Keep a read-only task/order binding even where this original task
            # has no mapped native write action. This lets the native evaluator
            # run against current ERP state without inventing an action.
            for order_id, source_order in original_db.get("orders", {}).items():
                native_name = native_orders.get(order_id)
                record = (
                    mapped_docs.get(("Sales Order", native_name))
                    if native_name
                    else None
                )
                if record is None or record.get("source_id") != order_id:
                    continue
                mapping_id = identity(
                    "tau2:retail",
                    experiment["experiment_id"],
                    task["id"],
                    "observe",
                    order_id,
                )
                if mapping_id in existing:
                    continue
                mapping = RecordMapping(
                    mapping_id=mapping_id,
                    benchmark="tau_retail",
                    upstream_commit=manifest["upstream"]["commit"],
                    source_record_id=order_id,
                    source_entity_type="order",
                    erp_doctype="Sales Order",
                    erp_name=native_name,
                    experiment_id=experiment["experiment_id"],
                    company_time=now,
                    native_context={
                        "task_id": task["id"],
                        "observation_only": True,
                        "source_snapshot": source_order,
                    },
                    message_ids=[message_id]
                    if isinstance(message_id, str) and message_id
                    else [],
                )
                self.pipeline.map_record(mapping, record["document"])
                existing.add(mapping_id)
            user_address_targets = {
                (action.get("arguments") or {}).get("user_id")
                for action in actions
                if isinstance(action, dict)
                and action.get("name") == "modify_user_address"
            }
            for user_id in user_address_targets:
                source_user = original_db.get("users", {}).get(user_id)
                if not isinstance(source_user, dict):
                    # The authoritative manifest's source_gaps reports dangling
                    # references; no replacement user is selected here.
                    continue
                native_name = native_addresses.get(f"user:{user_id}")
                record = mapped_docs.get(("Address", native_name)) if native_name else None
                customer_name = receipt.get("native_documents", {}).get(
                    "users", {}
                ).get(user_id)
                if not native_name or not customer_name:
                    raise RuntimeError(
                        "τ user-address task has no exact native Customer and Address import mapping"
                    )
                if record is None or record.get("source_id") != user_id:
                    raise RuntimeError(
                        "τ user-address native Address is missing from the company source evidence set"
                    )
                mapping_id = identity(
                    "tau2:retail", experiment["experiment_id"], task["id"],
                    "user-address", user_id,
                )
                if mapping_id in existing:
                    continue
                mapping = RecordMapping(
                    mapping_id=mapping_id,
                    benchmark="tau_retail",
                    upstream_commit=manifest["upstream"]["commit"],
                    source_record_id=user_id,
                    source_entity_type="user",
                    erp_doctype="Address",
                    erp_name=native_name,
                    experiment_id=experiment["experiment_id"],
                    company_time=now,
                    native_context={
                        "task_id": task["id"],
                        "native_action": "modify_user_address",
                        "events": ["on_update"],
                        "native_ids": {
                            "user_id": user_id,
                            "customer_name": customer_name,
                        },
                        "document_fields": {
                            "address1": "address_line1",
                            "address2": "address_line2",
                            "city": "city",
                            "state": "state",
                            "country": "country",
                            "zip": "pincode",
                        },
                        "source_snapshot": source_user,
                    },
                    message_ids=[message_id] if isinstance(message_id, str) and message_id else [],
                )
                self.pipeline.map_record(mapping, record["document"])
                existing.add(mapping_id)
            for index, action in enumerate(actions):
                if not isinstance(action, dict):
                    continue
                action_name = action.get("name")
                if action_name not in {
                    "cancel_pending_order",
                    "modify_pending_order_address",
                    "modify_pending_order_items",
                    "modify_pending_order_payment",
                    "return_delivered_order_items",
                    "exchange_delivered_order_items",
                }:
                    continue
                args = action.get("arguments") or {}
                order_id = args.get("order_id")
                if (
                    not isinstance(order_id, str)
                    or order_id not in original_db.get("orders", {})
                    or order_id not in native_orders
                ):
                    continue
                native_name = native_orders[order_id]
                record = mapped_docs.get(("Sales Order", native_name))
                if record is None or record.get("source_id") != order_id:
                    continue
                action_id = action.get("action_id") or str(index)
                mapping_id = identity(
                    "tau2:retail",
                    experiment["experiment_id"],
                    task["id"],
                    str(action_id),
                    order_id,
                )
                if mapping_id in existing:
                    continue
                if action_name in {
                    "return_delivered_order_items",
                    "exchange_delivered_order_items",
                }:
                    matching_issues = [
                        row
                        for row in records
                        if row.get("doctype") == "Issue"
                        and row.get("document", {}).get("custom_customer_service_order")
                        == native_name
                        and row.get("document", {}).get("custom_customer_service_type")
                        == (
                            "Return"
                            if action_name == "return_delivered_order_items"
                            else "Exchange"
                        )
                    ]
                    if len(matching_issues) != 1:
                        continue
                    issue_record = matching_issues[0]
                    mapping = RecordMapping(
                        mapping_id=mapping_id,
                        benchmark="tau_retail",
                        upstream_commit=manifest["upstream"]["commit"],
                        source_record_id=order_id,
                        source_entity_type="order",
                        erp_doctype="Issue",
                        erp_name=issue_record["name"],
                        experiment_id=experiment["experiment_id"],
                        company_time=now,
                        native_context={
                            "task_id": task["id"],
                            "native_action": action_name,
                            "events": ["on_update", "on_submit"],
                            "native_ids": {
                                "order_id": order_id,
                                "sales_order_name": native_name,
                            },
                            "document_fields": {},
                            "source_snapshot": original_db["orders"][order_id],
                        },
                        message_ids=[message_id]
                        if isinstance(message_id, str) and message_id
                        else [],
                    )
                    self.pipeline.map_record(mapping, issue_record["document"])
                    existing.add(mapping_id)
                    continue
                event_by_action = {
                    "cancel_pending_order": "on_cancel",
                    "modify_pending_order_address": "on_update_after_submit",
                    "modify_pending_order_items": "on_update_after_submit",
                    "modify_pending_order_payment": "on_update_after_submit",
                }
                context = {
                    "task_id": task["id"],
                    "native_action": action_name,
                    "events": [event_by_action[action_name]],
                    "native_ids": {"order_id": order_id},
                    "document_fields": {},
                    # Provenance only. The translator reads current observed
                    # ERP fields and never treats this as a completed action.
                    "source_snapshot": original_db["orders"][order_id],
                }
                if action_name == "cancel_pending_order":
                    context["document_fields"] = {
                        "before_state": "before_document.custom_customer_service_state",
                        "docstatus": "docstatus",
                        "current_state": "custom_customer_service_state",
                        "reason": "custom_customer_service_cancel_reason",
                    }
                message_id = prepared.get("initial_email", {}).get("message_id")
                mapping = RecordMapping(
                    mapping_id=mapping_id,
                    benchmark="tau_retail",
                    upstream_commit=manifest["upstream"]["commit"],
                    source_record_id=order_id,
                    source_entity_type="order",
                    erp_doctype="Sales Order",
                    erp_name=native_name,
                    experiment_id=experiment["experiment_id"],
                    company_time=now,
                    native_context=context,
                    message_ids=[message_id]
                    if isinstance(message_id, str) and message_id
                    else [],
                )
                self.pipeline.map_record(mapping, record["document"])
                existing.add(mapping_id)

    def _tau_retail_mapping_context(self, action):
        action_data = (
            action.model_dump(mode="json", by_alias=True)
            if hasattr(action, "model_dump")
            else action
        )
        if not isinstance(action_data, dict):
            raise TypeError(
                "τ retail feedback action must be a contract model or mapping"
            )
        mapping_id = action_data.get("mapping_id")
        mapping = next(
            (row for row in self.pipeline.mappings() if row.mapping_id == mapping_id),
            None,
        )
        if (
            mapping is None
            or mapping.benchmark != "tau_retail"
            or not mapping.experiment_id
        ):
            raise ValueError(
                "τ retail feedback action has no run-scoped source mapping"
            )
        task_id = mapping.native_context.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("τ retail feedback mapping has no original task ID")
        return task_id, mapping.experiment_id

    def _apply_tau_retail_action(self, action):
        task_id, experiment_id = self._tau_retail_mapping_context(action)
        return self.actor_client.apply_retail_feedback(
            task_id,
            experiment_id,
            action,
        )

    def _reconcile_tau_retail_action(self, action):
        task_id, experiment_id = self._tau_retail_mapping_context(action)
        return self.actor_client.reconcile_retail_feedback(
            task_id,
            experiment_id,
            action,
        )

    def _enrich_tau_retail_evidence(self, mapping, evidence):
        context = mapping.get("native_context", {})
        native_action = context.get("native_action")
        if native_action in {
            "modify_pending_order_address",
            "modify_pending_order_items",
            "modify_pending_order_payment",
        } and (
            evidence.get("event") != "on_update_after_submit"
            or mapping.get("erp_doctype") != "Sales Order"
        ):
            return evidence
        if (
            native_action == "cancel_pending_order"
            and evidence.get("event") != "on_cancel"
        ):
            return evidence
        if native_action in {
            "return_delivered_order_items",
            "exchange_delivered_order_items",
        }:
            if (
                evidence.get("event") not in {"on_update", "on_submit"}
                or mapping.get("erp_doctype") != "Issue"
            ):
                return evidence
        elif native_action not in {
            "cancel_pending_order",
            "modify_pending_order_address",
            "modify_pending_order_items",
            "modify_pending_order_payment",
        }:
            return evidence
        task_id, experiment_id = self._tau_retail_mapping_context(
            {"mapping_id": mapping.get("mapping_id")}
        )
        run = self.store.get_run(experiment_id)
        prepared = run.get("manifest", {}).get("prepared_problem", {})
        manifest = prepared.get("snapshot") if isinstance(prepared, dict) else None
        if not isinstance(manifest, dict) or manifest.get("task_id") != task_id:
            raise ValueError(
                "τ retail feedback has no matching prepared source manifest"
            )
        observation = self.retail_admin.observe_task(manifest, experiment_id)
        if (
            observation.get("task_id") != task_id
            or observation.get("experiment_id") != experiment_id
            or not isinstance(observation.get("observation"), dict)
        ):
            raise ValueError(
                "τ retail observer returned an invalid native projection"
            )
        order = (
            observation["observation"]
            .get("orders", {})
            .get(mapping.get("source_record_id"))
        )
        enriched = dict(evidence)
        if not isinstance(order, dict):
            return evidence
        evidence_by_id = observation.get("native_evidence", {})
        refs = (
            evidence_by_id.get(mapping.get("source_record_id"))
            if isinstance(evidence_by_id, dict)
            else None
        )
        if not isinstance(refs, list):
            refs = []
        source_order = context.get("source_snapshot")
        user_id = (
            source_order.get("user_id")
            if isinstance(source_order, dict)
            else order.get("user_id")
        )
        user = observation["observation"].get("users", {}).get(user_id)
        service_issue = (
            observation.get("service_issue_evidence", {}).get(
                mapping.get("source_record_id")
            )
            if isinstance(observation.get("service_issue_evidence"), dict)
            else None
        )
        if native_action in {
            "return_delivered_order_items",
            "exchange_delivered_order_items",
        }:
            if not isinstance(service_issue, dict) or service_issue.get(
                "name"
            ) != mapping.get("erp_name"):
                return evidence
            refs = [*refs, service_issue.get("native_ref")]
        enriched["native_observation"] = {
            "order": order,
            "user": user,
            "native_evidence": refs,
            "service_issue": service_issue,
        }
        observed_at = self.company_time()
        if observed_at is not None:
            enriched["native_observed_company_time"] = observed_at.isoformat()
        return enriched

    def _evaluate_tau_retail(self, mappings, evidence):
        experiments = {mapping.experiment_id for mapping in mappings}
        tasks = {mapping.native_context.get("task_id") for mapping in mappings}
        if len(experiments) != 1 or len(tasks) != 1 or None in tasks:
            return {
                "status": "inconclusive",
                "reason": "τ retail evaluation spans multiple task executions",
            }
        experiment_id = next(iter(experiments))
        task_id = next(iter(tasks))
        run = self.store.get_run(experiment_id)
        prepared = run.get("manifest", {}).get("prepared_problem", {})
        manifest = prepared.get("snapshot") if isinstance(prepared, dict) else None
        if not isinstance(manifest, dict) or manifest.get("task_id") != task_id:
            return {
                "status": "inconclusive",
                "reason": "τ retail prepared task snapshot is unavailable",
            }
        native = self.retail_admin.observe_task(manifest, experiment_id)
        if (
            native.get("task_id") != task_id
            or native.get("experiment_id") != experiment_id
            or not isinstance(native.get("observation"), dict)
            or not isinstance(native.get("unmapped"), list)
        ):
            raise ValueError(
                "native ERPNext τ retail observer returned an invalid task projection"
            )
        actors = self.store.list_actors(run["run_id"], run["generation"])
        customer_ids = {
            actor["actor_id"] for actor in actors if actor.get("kind") == "customer"
        }
        messages_by_id = {}

        def message_time(value):
            if not value:
                return datetime.min.replace(tzinfo=timezone.utc)
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (
                parsed
                if parsed.tzinfo is not None
                else parsed.replace(tzinfo=timezone.utc)
            )

        for item in self.store.list_outbox(
            run["run_id"], run["generation"], status="completed"
        ):
            message = item["private_payload"]
            message_id = message.get("message_id")
            if not message_id:
                continue
            if message.get("from_actor_id") in customer_ids and message.get(
                "to_actor_id"
            ) in {prepared.get("support_actor_id"), run.get("agent_id")}:
                role = "user"
            elif (
                message.get("from_actor_id")
                in {prepared.get("support_actor_id"), run.get("agent_id")}
                and message.get("to_actor_id") in customer_ids
            ):
                role = "assistant"
            else:
                continue
            messages_by_id[message_id] = (
                message_time(message.get("simulated_time", run["simulated_time"])),
                item["outbox_id"],
                {"role": role, "content": str(message.get("body", ""))},
            )
        for message in self.store.list_private_messages(
            run["run_id"], run["generation"]
        ):
            if not message.get("message_id"):
                continue
            sender, recipient = message.get("from_actor_id"), message.get("to_actor_id")
            if sender in customer_ids and recipient == prepared.get("support_actor_id"):
                role = "user"
            elif (
                sender in {prepared.get("support_actor_id"), run.get("agent_id")}
                and recipient in customer_ids
            ):
                role = "assistant"
            else:
                continue
            messages_by_id[message["message_id"]] = (
                message_time(message.get("simulated_time")),
                f"received:{message['message_id']}",
                {"role": role, "content": str(message.get("body", ""))},
            )
        evaluation = self.actor_client.evaluate_retail_feedback(
            task_id,
            experiment_id,
            native["observation"],
            native["unmapped"],
            [
                row[2]
                for row in sorted(
                    messages_by_id.values(), key=lambda row: (row[0], row[1])
                )
            ],
        )
        return {"source_gaps": native.get("source_gaps", []), "evaluation": evaluation}

    def _link_issue_threads(self):
        with self.store.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_issue_handoffs'"
            ).fetchone():
                return
            rows = [
                dict(row) for row in db.execute("SELECT * FROM company_issue_handoffs")
            ]
        mappings = self.pipeline.mappings()
        for row in rows:
            notification = json.loads(row["notification_json"])
            documents = {
                (order["doctype"], order["name"])
                for order in notification["affected_orders"]
            }
            message_ids = [notification["message_id"]]
            if row["handoff_json"]:
                message_ids.append(json.loads(row["handoff_json"])["message_id"])
            for mapping in mappings:
                if (mapping.erp_doctype, mapping.erp_name) in documents:
                    for message_id in message_ids:
                        self.pipeline.link_message(mapping.mapping_id, message_id)

    def _mail_evidence_exists(self, message_id, scope="mail"):
        with self.store.transaction() as db:
            return (
                db.execute(
                    "SELECT 1 FROM company_evidence WHERE evidence_id=?",
                    (identity(scope, message_id),),
                ).fetchone()
                is not None
            )

    def _link_coffee_outbox_message(self, message: dict) -> None:
        reference = message.get("related_document")
        if reference is None:
            return
        if not isinstance(reference, dict) or set(reference) != {"doctype", "name"}:
            raise ValueError("CoffeeBench email has an invalid related company-document reference")
        mappings = [
            mapping for mapping in self.pipeline.mappings()
            if mapping.benchmark == "coffeebench"
            and mapping.erp_doctype == reference["doctype"]
            and mapping.erp_name == reference["name"]
        ]
        if len(mappings) != 1:
            raise ValueError("CoffeeBench email reference must resolve to one current company source mapping")
        mapping = mappings[0]
        route = mapping.native_context.get("email_reply")
        if (
            not isinstance(route, dict)
            or message.get("to_actor_id")
            not in route.get("allowed_recipient_actor_ids", [])
        ):
            raise ValueError("CoffeeBench email recipient does not match its mapped counterparty")
        message_id = message.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("CoffeeBench email has no stable delivered message ID")
        self.pipeline.link_message(mapping.mapping_id, message_id)

    def deliver_native_messages(self, raw_events):
        """Deliver native Coffee counterparty messages to the mapped employee inbox."""
        failures = []
        mapping_gaps = []
        attempted_source_ids = set()
        for raw in raw_events:
            source_event_id = raw.get("event_id") if isinstance(raw, dict) else None
            native_message_id = None
            try:
                if not isinstance(raw, dict):
                    raise ValueError("CoffeeBench source event must be an object")
                if raw.get("event_type") != "COFFEE_NATIVE_MESSAGE_RECEIVED":
                    continue
                event = CompanySourceEvent.model_validate(raw)
                source_event_id = event.event_id
                native_message_id = event.payload.get("native_message_id")
                plan = self._coffee_native_message_plan(event)
                self._deliver_coffee_native_message(event, plan)
                attempted_source_ids.add(event.event_id)
                if plan["mapping_status"] != "mapped":
                    mapping_gaps.append({
                        "source_event_id": event.event_id,
                        "native_message_id": event.payload["native_message_id"],
                        "error_code": plan["mapping_status"],
                    })
            except Exception as error:
                if isinstance(source_event_id, str):
                    attempted_source_ids.add(source_event_id)
                failures.append({
                    "source_event_id": source_event_id,
                    "native_message_id": native_message_id if isinstance(native_message_id, str) else "unknown",
                    "error_type": type(error).__name__,
                })
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                causes = []
                cause = error.__cause__ or error.__context__
                while cause is not None:
                    causes.append(type(cause).__name__)
                    cause = cause.__cause__ or cause.__context__
                LOG.error(
                    "CoffeeBench native message delivery failed source_event_id=%s native_message_id=%s "
                    "exception_type=%s causes=%s stack=%s",
                    source_event_id, native_message_id, type(error).__name__, causes, frames,
                )
        # The background provider may acknowledge its cursor once source events
        # are durably handed off. Replay only private outbox rows that have not
        # reached a recipient receipt, reconstructing from the frozen payload.
        with self.store.transaction() as db:
            pending_rows = [dict(row) for row in db.execute(
                "SELECT * FROM company_native_message_outbox WHERE status IN ('pending','submitting','uncertain') ORDER BY source_event_id"
            )]
        for row in pending_rows:
            if row["source_event_id"] in attempted_source_ids:
                continue
            attempted_source_ids.add(row["source_event_id"])
            try:
                plan = self._coffee_native_message_plan_from_outbox(row)
                self._deliver_coffee_native_message(None, plan)
                if plan["mapping_status"] != "mapped":
                    mapping_gaps.append({
                        "source_event_id": row["source_event_id"],
                        "native_message_id": plan["native_message"]["native_message_id"],
                        "error_code": plan["mapping_status"],
                    })
            except Exception as error:
                attempted_source_ids.add(row["source_event_id"])
                # A malformed saved payload may be the original failure. Do not
                # parse it again while reporting and hide that failure.
                failures.append({
                    "source_event_id": row["source_event_id"],
                    "native_message_id": "unknown",
                    "error_type": type(error).__name__,
                })
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error(
                    "CoffeeBench pending native message retry failed source_event_id=%s exception_type=%s stack=%s",
                    row["source_event_id"], type(error).__name__, frames,
                )
        # Source events are the write-ahead input. If planning failed before an
        # SMTP outbox row existed, retry that exact persisted event on the next
        # mail wake. Delivered outbox rows are terminal and are never resubmitted.
        with self.store.transaction() as db:
            saved_outbox_rows = [dict(row) for row in db.execute(
                "SELECT * FROM company_native_message_outbox ORDER BY source_event_id"
            )]
        outbox_by_source = {row["source_event_id"]: row for row in saved_outbox_rows}
        for raw in self.store.list_company_source_events():
            if (
                raw.get("source") != "sakanaai-coffeebench-native-economy"
                or raw.get("event_type") != "COFFEE_NATIVE_MESSAGE_RECEIVED"
                or raw.get("event_id") in attempted_source_ids
            ):
                continue
            source_event_id = raw["event_id"]
            row = outbox_by_source.get(source_event_id)
            if row is not None and row["status"] == "delivered":
                continue
            try:
                if row is not None:
                    plan = self._coffee_native_message_plan_from_outbox(row)
                else:
                    event = CompanySourceEvent.model_validate(raw)
                    plan = self._coffee_native_message_plan(event)
                attempted_source_ids.add(source_event_id)
                self._deliver_coffee_native_message(None, plan)
            except Exception as error:
                attempted_source_ids.add(source_event_id)
                native_id = raw.get("payload", {}).get("native_message_id", "unknown")
                failures.append({
                    "source_event_id": source_event_id,
                    "native_message_id": native_id if isinstance(native_id, str) else "unknown",
                    "error_type": type(error).__name__,
                })
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error(
                    "CoffeeBench persisted source-message retry failed source_event_id=%s exception_type=%s stack=%s",
                    source_event_id, type(error).__name__, frames,
                )
        # Keep financial-attribution gaps visible after the source message has
        # been delivered; an empty new tick must not erase unresolved history.
        with self.store.transaction() as db:
            delivered_unmapped = [dict(row) for row in db.execute(
                "SELECT source_event_id,message_id,private_payload_json FROM company_native_message_outbox "
                "WHERE status='delivered' AND mapping_id IS NULL ORDER BY source_event_id"
            )]
        mapping_gaps = []
        for row in delivered_unmapped:
            payload = json.loads(row["private_payload_json"])
            native = payload.get("native_source_message") or {}
            mapping_gaps.append({
                "source_event_id": row["source_event_id"],
                "native_message_id": native.get("native_message_id", "unknown"),
                "error_code": payload.get("mapping_status", "unmapped_native_message_source"),
            })
        component = "component_coffee_native_mail"
        with self.store.transaction() as db:
            if failures or mapping_gaps:
                db.execute(
                    "INSERT INTO company_feedback_runtime_reports VALUES(?,?) "
                    "ON CONFLICT(provider) DO UPDATE SET result_json=excluded.result_json",
                    (component, json.dumps({
                        "status": "partial",
                        "failures": failures,
                        "mapping_gaps": mapping_gaps,
                    }, sort_keys=True)),
                )
            else:
                db.execute("DELETE FROM company_feedback_runtime_reports WHERE provider=?", (component,))
        return {"status": "partial" if failures or mapping_gaps else "ok", "failures": failures}

    def _coffee_native_message_plan_from_outbox(self, row: dict) -> dict:
        payload = json.loads(row["private_payload_json"])
        native = payload.get("native_source_message")
        if not isinstance(native, dict) or not isinstance(payload.get("sender"), dict) or not isinstance(payload.get("recipient"), dict):
            raise ValueError("Coffee native message outbox has no immutable retry context")
        if not all(isinstance(native.get(key), str) and native[key] for key in
                   ("native_message_id", "sender_id", "recipient_id", "title", "body")):
            raise ValueError("Coffee native message outbox source record is incomplete")
        mapping_id = row.get("mapping_id")
        mapping = next((item for item in self.pipeline.mappings()
                        if item.mapping_id == mapping_id and item.benchmark == "coffeebench"), None) if mapping_id else None
        if mapping_id and mapping is None:
            raise ValueError("Coffee native message retry has no matching company source mapping")
        return {
            "source_id": row["source_event_id"],
            "mapping_id": mapping_id,
            "erp_doctype": mapping.erp_doctype if mapping else None,
            "erp_name": mapping.erp_name if mapping else None,
            "native_message": native,
            "sender": payload["sender"],
            "recipient": payload["recipient"],
            "in_reply_to": payload.get("in_reply_to"),
            "mapping_status": payload.get("mapping_status") or (
                "mapped" if mapping else "unmapped_native_message_source"
            ),
            "message_id": row["message_id"],
            "run_id": payload["run_id"],
            "generation": payload["generation"],
            "occurred_at": payload.get("occurred_at"),
        }

    def _coffee_native_message_plan(self, event: CompanySourceEvent) -> dict:
        if event.source != "sakanaai-coffeebench-native-economy" or event.upstream_commit != "9e2f3950b5812c8753de81ede9c68babc024b5c8":
            raise ValueError("CoffeeBench native message provenance is not pinned")
        payload = event.payload
        required = (
            "native_message_id", "sender_id", "recipient_id", "title", "body",
            "sent_at_virtual_minute",
        )
        if any(key not in payload for key in required):
            raise ValueError("CoffeeBench native message source record is incomplete")
        message_refs = {
            str(payload[key]) for key in ("ref_listing_id", "ref_offer_id", "ref_deal_id")
            if isinstance(payload.get(key), str) and payload[key]
        }
        profile = json.loads(self.config.company_profile_file.read_text(encoding="utf-8"))
        coffee = profile["company"]["background"]["coffee"]
        configured_actors = {actor["actor_id"]: actor for actor in self.config.mail["actors"]}
        if payload["recipient_id"] not in coffee["company_business_ids"]:
            raise ValueError("CoffeeBench native message is not addressed to the mapped company")
        counterparty = coffee.get("counterparties", {}).get(payload["sender_id"])
        if not isinstance(counterparty, dict) or counterparty.get("actor_id") not in configured_actors:
            raise ValueError("CoffeeBench counterparty has no configured Mailu identity")
        sender = configured_actors[counterparty["actor_id"]]
        if (
            sender.get("kind") != "counterparty"
            or sender.get("email", "").casefold() != str(counterparty.get("email", "")).casefold()
        ):
            raise ValueError("CoffeeBench counterparty Mailu identity differs from its company directory entry")
        mailbox_id = coffee.get("mail_sender_actor_id")
        recipient = configured_actors.get(mailbox_id)
        if not recipient or recipient.get("kind") not in {"employee", "warehouse", "logistics", "approver"}:
            raise ValueError("CoffeeBench native message has no configured company mailbox")
        mappings = []
        for mapping in self.pipeline.mappings():
            if mapping.benchmark != "coffeebench":
                continue
            route = mapping.native_context.get("email_reply")
            if not isinstance(route, dict) or route.get("counterparty_id") != payload["sender_id"]:
                continue
            native_ids = route.get("native_ids", {})
            source_native_id = route.get("source_native_record_id")
            mapped_refs = {
                str(value) for key, value in native_ids.items()
                if key.startswith("ref_") and isinstance(value, str) and value
            }
            if isinstance(source_native_id, str) and source_native_id:
                mapped_refs.add(source_native_id)
            if not message_refs or not message_refs.intersection(mapped_refs):
                continue
            if route.get("actor_id") != payload["recipient_id"]:
                continue
            mappings.append(mapping)
        mapping = mappings[0] if len(mappings) == 1 else None
        mapping_status = "mapped" if mapping else (
            "ambiguous_native_erp_mapping" if len(mappings) > 1
            else "unmapped_native_message_source"
        )
        root = self._coffee_native_message_root(mapping, payload)
        source_id = event.event_id
        message_id = "<coffee-native-" + hashlib.sha256(source_id.encode()).hexdigest() + "@theagentcompany.local>"
        return {
            "source_id": source_id,
            "mapping_id": mapping.mapping_id if mapping else None,
            "erp_doctype": mapping.erp_doctype if mapping else None,
            "erp_name": mapping.erp_name if mapping else None,
            "native_message": payload,
            "sender": {"actor_id": sender["actor_id"], "email": sender["email"]},
            "recipient": {"actor_id": recipient["actor_id"], "email": recipient["email"]},
            "in_reply_to": root,
            "mapping_status": mapping_status,
            "message_id": message_id,
            "run_id": "company-coffeebench-native",
            "generation": 0,
            "occurred_at": event.simulated_time.isoformat(),
        }

    def _coffee_native_message_root(self, mapping, message) -> str:
        source_refs = {
            str(message[key]) for key in ("ref_listing_id", "ref_offer_id", "ref_deal_id")
            if isinstance(message.get(key), str) and message[key]
        }
        if mapping is None:
            # Conversation-only actions have no ERP mapping. Bind a later
            # native message to the exact previously delivered native thread
            # only when actor direction and source references identify one.
            if not source_refs:
                return None
            with self.store.transaction() as db:
                rows = [dict(row) for row in db.execute(
                    "SELECT a.native_actor_id,a.native_recipient_id,a.native_refs_json,a.mail_receipt_json,o.message_id "
                    "FROM company_coffee_thread_actions a JOIN company_native_message_outbox o "
                    "ON o.source_event_id=a.source_event_id WHERE a.status='applied' AND o.status='delivered'"
                )]
            matches = []
            for row in rows:
                refs = json.loads(row["native_refs_json"] or "{}")
                native_refs = {str(value) for value in refs.values() if isinstance(value, str) and value}
                if (
                    row["native_actor_id"] == message.get("recipient_id")
                    and row["native_recipient_id"] == message.get("sender_id")
                    and source_refs.intersection(native_refs)
                ):
                    matches.append(row["message_id"])
            return matches[0] if len(set(matches)) == 1 else None
        with self.store.transaction() as db:
            actions = [dict(row) for row in db.execute(
                "SELECT evidence_id,translation_json,receipt_json FROM company_feedback_actions "
                "WHERE mapping_id=? AND stage='reconciled' ORDER BY rowid", (mapping.mapping_id,),
            )]
            evidence_by_id = {
                row["evidence_id"]: CompanyEvidence.model_validate_json(row["payload_json"])
                for row in db.execute(
                    "SELECT evidence_id,payload_json FROM company_evidence WHERE evidence_id IN "
                    "(SELECT evidence_id FROM company_feedback_actions WHERE mapping_id=? AND stage='reconciled')",
                    (mapping.mapping_id,),
                )
            }
        matches = []
        route = mapping.native_context["email_reply"]
        for row in actions:
            translation = json.loads(row["translation_json"])
            action = translation.get("action")
            receipt = json.loads(row["receipt_json"] or "{}")
            outcome = receipt.get("outcome", {})
            native_result = outcome.get("native_result", {}) if isinstance(outcome, dict) else {}
            if (
                not isinstance(action, dict)
                or action.get("native_action") != "send_message"
                or action.get("actor_id") != message["recipient_id"]
                or action.get("arguments", {}).get("recipient") != message["sender_id"]
                or not isinstance(native_result, dict)
                or not native_result.get("message_id")
            ):
                continue
            action_refs = {
                str(action["arguments"][key]) for key in ("ref_listing_id", "ref_offer_id")
                if isinstance(action.get("arguments", {}).get(key), str)
            }
            if not source_refs or not source_refs.intersection(action_refs):
                continue
            evidence = evidence_by_id.get(row["evidence_id"])
            if evidence is None or evidence.actor not in route.get("allowed_mailbox_actor_ids", []):
                continue
            if evidence.recipient_actor_id not in route.get("allowed_recipient_actor_ids", []):
                continue
            source_message_id = evidence.message_id
            document = evidence.document
            if (
                source_message_id not in mapping.message_ids
                or document.get("related_document") != {"doctype": mapping.erp_doctype, "name": mapping.erp_name}
                or document.get("subject") != action.get("arguments", {}).get("title")
                or document.get("body") != action.get("arguments", {}).get("body")
            ):
                continue
            thread_root = evidence.in_reply_to or source_message_id
            if thread_root in mapping.message_ids:
                matches.append(thread_root)
        return matches[0] if len(set(matches)) == 1 else None

    def _deliver_coffee_native_message(self, event: CompanySourceEvent | None, plan: dict) -> None:
        from .mail import MailDeliveryUnconfirmed, MailuGateway
        from .runtime import private_json
        from .company_feedback import encoded, identity

        if self._mail_gateway is None:
            self._mail_gateway = MailuGateway(
                self.config.mail, private_json(self.config.mail_credentials_file)
            )
        payload = {
            "sender": plan["sender"],
            "recipient": plan["recipient"],
            "subject": plan["native_message"]["title"],
            "body": plan["native_message"]["body"],
            "message_id": plan["message_id"],
            "run_id": plan["run_id"],
            "generation": plan["generation"],
            "in_reply_to": plan["in_reply_to"],
            # Keep the exact native source identity beside the delivered root.
            # This is needed to continue native conversations when there is no
            # ERP mapping; it is not exported as a company financial record.
            "native_source_message": plan["native_message"],
            "native_actor_id": plan["native_message"]["recipient_id"],
            "native_counterparty_id": plan["native_message"]["sender_id"],
            "mapping_id": plan["mapping_id"],
            "erp_doctype": plan["erp_doctype"],
            "erp_name": plan["erp_name"],
            "occurred_at": plan["occurred_at"],
            "mapping_status": plan["mapping_status"],
        }
        request_hash = hashlib.sha256(encoded(payload).encode("utf-8")).hexdigest()
        locks = self.store.db_path.parent / "company-native-message-locks"
        locks.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = locks / (hashlib.sha256(plan["source_id"].encode()).hexdigest() + ".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            now = datetime.now(timezone.utc).isoformat()
            with self.store.transaction() as db:
                row = db.execute(
                    "SELECT * FROM company_native_message_outbox WHERE source_event_id=?",
                    (plan["source_id"],),
                ).fetchone()
                if row is None:
                    db.execute(
                        "INSERT INTO company_native_message_outbox(source_event_id,mapping_id,message_id,request_hash,private_payload_json,status,updated_at) "
                        "VALUES(?,?,?,?,?,'pending',?)",
                        (plan["source_id"], plan["mapping_id"], plan["message_id"], request_hash, encoded(payload), now),
                    )
                    status, submission_started = "pending", False
                else:
                    previous_payload = json.loads(row["private_payload_json"])
                    content_fields = (
                        "sender", "recipient", "subject", "body", "message_id", "run_id", "generation",
                        "native_source_message", "native_actor_id", "native_counterparty_id",
                        "mapping_id", "erp_doctype", "erp_name", "occurred_at", "mapping_status",
                    )
                    if any(previous_payload.get(key) != payload.get(key) for key in content_fields):
                        raise ValueError("CoffeeBench native email source ID was reused with changed message facts")
                    if row["mapping_id"] and plan["mapping_id"] and row["mapping_id"] != plan["mapping_id"]:
                        raise ValueError("CoffeeBench native email source ID changed its mapped company record")
                    status, submission_started = row["status"], bool(row["submission_started"])
                    if status == "pending" and not submission_started:
                        db.execute(
                            "UPDATE company_native_message_outbox SET mapping_id=?,request_hash=?,private_payload_json=?,updated_at=? "
                            "WHERE source_event_id=?",
                            (plan["mapping_id"] or row["mapping_id"], request_hash, encoded(payload), now, plan["source_id"]),
                        )
                    else:
                        # Once an SMTP submission starts, preserve the actual
                        # thread root even if later evidence adds a mapping.
                        payload = previous_payload
                        request_hash = row["request_hash"]
                        if row["mapping_id"] is None and plan["mapping_id"] is not None:
                            db.execute(
                                "UPDATE company_native_message_outbox SET mapping_id=?,updated_at=? WHERE source_event_id=?",
                                (plan["mapping_id"], now, plan["source_id"]),
                            )
            if status in {"submitting", "uncertain"}:
                receipt = self._mail_gateway.find_receipt(
                    plan["recipient"], plan["message_id"], payload["subject"],
                    plan["run_id"], plan["generation"],
                )
                if receipt is None:
                    raise MailDeliveryUnconfirmed("native Coffee message submission remains unconfirmed")
            elif status == "pending":
                def submission_started():
                    with self.store.transaction() as db:
                        db.execute(
                            "UPDATE company_native_message_outbox SET status='submitting',submission_started=1,updated_at=? "
                            "WHERE source_event_id=? AND status='pending' AND submission_started=0",
                            (datetime.now(timezone.utc).isoformat(), plan["source_id"]),
                        )
                try:
                    receipt = self._mail_gateway.send(
                        plan["sender"], plan["recipient"], payload["subject"], payload["body"],
                        payload["message_id"], payload["run_id"], payload["generation"],
                        in_reply_to=payload["in_reply_to"],
                        on_submission_started=submission_started,
                    )
                except Exception as error:
                    with self.store.transaction() as db:
                        claimed = db.execute(
                            "SELECT submission_started FROM company_native_message_outbox WHERE source_event_id=?",
                            (plan["source_id"],),
                        ).fetchone()
                        db.execute(
                            "UPDATE company_native_message_outbox SET status=?,error_type=?,updated_at=? "
                            "WHERE source_event_id=?",
                            ("uncertain" if claimed and claimed["submission_started"] else "pending", type(error).__name__,
                             datetime.now(timezone.utc).isoformat(), plan["source_id"]),
                        )
                    raise
            elif status == "delivered":
                with self.store.transaction() as db:
                    row = db.execute(
                        "SELECT receipt_json FROM company_native_message_outbox WHERE source_event_id=?",
                        (plan["source_id"],),
                    ).fetchone()
                receipt = json.loads(row["receipt_json"])
            else:
                raise ValueError("CoffeeBench native email outbox has an invalid state")
            delivery = self._mail_gateway.inspect_issue_notification(
                plan["recipient"], payload["message_id"]
            )
            if delivery is None:
                raise MailDeliveryUnconfirmed("native Coffee message has no confirmed Mailu recipient receipt")
            document = {
                **plan["native_message"],
                "message_id": payload["message_id"],
                "subject": payload["subject"],
                "body": payload["body"],
                "from_actor_id": plan["sender"]["actor_id"],
                "to_actor_id": plan["recipient"]["actor_id"],
                "related_document": (
                    {"doctype": plan["erp_doctype"], "name": plan["erp_name"]}
                    if plan["mapping_id"] else None
                ),
                "in_reply_to": payload["in_reply_to"],
                "native_source_id": plan["native_message"]["native_message_id"],
                "native_source_time": plan["occurred_at"],
                "delivery_reference": delivery["reference"],
                "mailbox_uid": delivery["mailbox_uid"],
                "uidvalidity": delivery["uidvalidity"],
            }
            if plan["mapping_id"]:
                evidence = CompanyEvidence(
                    evidence_id=identity("coffee-native-message", payload["message_id"]),
                    erp_doctype=plan["erp_doctype"], erp_name=plan["erp_name"],
                    event="email_reply", actor=plan["sender"]["actor_id"],
                    occurred_at=delivery["received_at"], company_time=plan["occurred_at"],
                    document=document, evidence_refs=[delivery["reference"]],
                    message_id=payload["message_id"], recipient_actor_id=plan["recipient"]["actor_id"],
                    in_reply_to=payload["in_reply_to"],
                )
                self.pipeline.capture(evidence)
                self.pipeline.link_message(plan["mapping_id"], payload["message_id"])
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE company_native_message_outbox SET status='delivered',error_type=NULL,receipt_json=?,updated_at=? "
                    "WHERE source_event_id=?",
                    (encoded(delivery), datetime.now(timezone.utc).isoformat(), plan["source_id"]),
                )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def observe_mail(self):
        provider_error = None
        try:
            self._observe_registered_provider_mail()
        except Exception as error:
            provider_error = error
        coffee_error = None
        try:
            self._observe_coffee_company_replies()
        except Exception as error:
            coffee_error = error
        self._observe_issue_mail()
        for experiment in self.store.company_experiments():
            run = self.store.get_run(experiment["experiment_id"])
            for message in self.store.list_private_messages(
                run["run_id"], run["generation"]
            ):
                # Old messages without a real observation timestamp remain explicit
                # legacy evidence, never assigned a fabricated delivery instant.
                if not message.get("observed_at"):
                    continue
                message_id = message["message_id"]
                if self._mail_evidence_exists(message_id):
                    continue
                evidence = CompanyEvidence(
                    evidence_id=identity("mail", message_id),
                    erp_doctype="Communication",
                    erp_name=message_id,
                    event="email_reply"
                    if message.get("in_reply_to")
                    else "email_delivered",
                    actor=message["from_actor_id"],
                    occurred_at=message["observed_at"],
                    company_time=message["simulated_time"],
                    document=message,
                    evidence_refs=[message.get("webmail_url") or message["reference"]],
                    message_id=message_id,
                    in_reply_to=message.get("in_reply_to"),
                    recipient_actor_id=message["to_actor_id"],
                    experiment_id=str(run["run_id"]),
                )
                self.pipeline.observe(evidence)

            for outbox in self.store.list_outbox(
                run["run_id"], run["generation"], status="completed"
            ):
                message = outbox["private_payload"]
                message_id = message["message_id"]
                if self._mail_evidence_exists(
                    message_id, "sent-mail"
                ) or not outbox.get("completed_at"):
                    continue
                evidence = CompanyEvidence(
                    evidence_id=identity("sent-mail", message_id),
                    erp_doctype="Communication",
                    erp_name=message_id,
                    event="email_reply"
                    if message.get("in_reply_to")
                    else "email_delivered",
                    actor=message["from_actor_id"],
                    authenticated_sender_actor_id=message["from_actor_id"],
                    recipient_actor_id=message["to_actor_id"],
                    occurred_at=outbox["completed_at"],
                    company_time=message.get("simulated_time") or run["simulated_time"],
                    document=message,
                    evidence_refs=["outbox:" + outbox["outbox_id"]],
                    message_id=message_id,
                    in_reply_to=message.get("in_reply_to"),
                    experiment_id=str(run["run_id"]),
                )
                self._link_coffee_outbox_message(message)
                self.pipeline.observe(evidence)
        errors = [error for error in (provider_error, coffee_error) if error is not None]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Company registered mail observers failed", errors)

    def _observe_registered_provider_mail(self):
        if self.provider_mail_observer is None:
            return
        component = "component_provider_mail"
        try:
            result = self.provider_mail_observer()
            if isinstance(result, dict) and result.get("status") == "partial":
                failures = result.get("failures")
                if (
                    not isinstance(failures, list)
                    or any(
                        not isinstance(item, dict)
                        or not isinstance(item.get("provider_actor_id"), str)
                        or not isinstance(item.get("error_code"), str)
                        for item in failures
                    )
                ):
                    raise ValueError("Provider mail observer returned an invalid partial result")
            elif not isinstance(result, dict) or result.get("status") != "ok":
                raise ValueError("Provider mail observer returned an invalid result")
        except Exception as error:
            error.add_note("Company feedback component: registered provider mail")
            frames = [(frame.filename, frame.lineno, frame.name)
                      for frame in traceback.extract_tb(error.__traceback__)]
            causes = []
            cause = error.__cause__ or error.__context__
            while cause is not None:
                causes.append(type(cause).__name__)
                cause = cause.__cause__ or cause.__context__
            LOG.error(
                "registered provider mail observer failed exception_type=%s causes=%s stack=%s",
                type(error).__name__, causes, frames,
            )
            try:
                with self.store.transaction() as db:
                    db.execute(
                        "INSERT INTO company_feedback_runtime_reports VALUES(?,?) "
                        "ON CONFLICT(provider) DO UPDATE SET result_json=excluded.result_json",
                        (component, json.dumps({
                            "status": "failed",
                            "error_type": type(error).__name__,
                        }, sort_keys=True)),
                    )
            except Exception as report_error:
                error.add_note(
                    "Registered provider mail status recording failed: "
                    + type(report_error).__name__
                )
                raise error
            return
        if isinstance(result, dict) and result.get("status") == "partial":
            failures = result.get("failures")
            with self.store.transaction() as db:
                db.execute(
                    "INSERT INTO company_feedback_runtime_reports VALUES(?,?) "
                    "ON CONFLICT(provider) DO UPDATE SET result_json=excluded.result_json",
                    (component, json.dumps({
                        "status": "partial",
                        "failures": failures,
                    }, sort_keys=True)),
                )
            return
        with self.store.transaction() as db:
            db.execute(
                "DELETE FROM company_feedback_runtime_reports WHERE provider=?",
                (component,),
            )

    def _get_mail_gateway(self):
        if self._mail_gateway is None:
            from .mail import MailuGateway
            from .runtime import private_json

            self._mail_gateway = MailuGateway(
                self.config.mail, private_json(self.config.mail_credentials_file)
            )
        return self._mail_gateway

    def _observe_coffee_company_replies(self):
        """Import only authenticated employee replies delivered to mapped counterparties."""
        from .mail import MailReceiptMismatch

        profile = json.loads(self.config.company_profile_file.read_text(encoding="utf-8"))
        actor_by_id = {actor["actor_id"]: actor for actor in self.config.mail["actors"]}
        mappings = [
            mapping for mapping in self.pipeline.mappings()
            if mapping.benchmark == "coffeebench"
            and isinstance(mapping.native_context.get("email_reply"), dict)
            and mapping.message_ids
        ]
        root_mappings: dict[str, set[str]] = {}
        for mapping in mappings:
            for message_id in mapping.message_ids:
                root_mappings.setdefault(message_id, set()).add(mapping.mapping_id)
        failures = []
        gateway = self._get_mail_gateway()
        for mapping in mappings:
            route = mapping.native_context["email_reply"]
            external_actor_id = route.get("allowed_recipient_actor_ids", [None])[0]
            mailbox = actor_by_id.get(external_actor_id)
            sender_ids = route.get("allowed_mailbox_actor_ids")
            if (
                mailbox is None or mailbox.get("kind") != "counterparty"
                or not isinstance(sender_ids, list) or not sender_ids
            ):
                failures.append({"mapping_id": mapping.mapping_id, "error_type": "CoffeeMailRouteInvalid"})
                continue
            with self.store.transaction() as db:
                cursor = db.execute(
                    "SELECT uidvalidity,after_uid FROM company_coffee_mail_cursors "
                    "WHERE mapping_id=? AND mailbox_actor_id=?",
                    (mapping.mapping_id, mailbox["actor_id"]),
                ).fetchone()
            after_uid = int(cursor["after_uid"]) if cursor else 0
            expected_uidvalidity = str(cursor["uidvalidity"]) if cursor else None
            try:
                messages = gateway.poll_registered_replies(
                    mailbox, set(mapping.message_ids), after_uid, set(sender_ids),
                    expected_uidvalidity=expected_uidvalidity,
                    require_authenticated_submission=True,
                )
                for message in messages:
                    message_id = message["message_id"]
                    in_reply_to = message.get("in_reply_to")
                    referenced = {
                        in_reply_to,
                        *re.findall(r"<[^<>\s]+>", message.get("references", "")),
                    }
                    matched = set().union(*(
                        root_mappings.get(root, set()) for root in referenced if root
                    ))
                    if matched != {mapping.mapping_id} or in_reply_to not in mapping.message_ids:
                        raise MailReceiptMismatch(
                            "CoffeeBench company email is not linked to exactly one mapped native thread"
                        )
                    sender_id = message.get("authenticated_sender_actor_id")
                    if sender_id != message.get("sender_actor_id") or sender_id not in sender_ids:
                        raise MailReceiptMismatch(
                            "CoffeeBench company email has no matching Mailu authenticated-submission identity"
                        )
                    evidence = CompanyEvidence(
                        evidence_id=identity("mail", message_id),
                        erp_doctype=mapping.erp_doctype,
                        erp_name=mapping.erp_name,
                        event="email_reply",
                        actor=sender_id,
                        authenticated_sender_actor_id=sender_id,
                        recipient_actor_id=mailbox["actor_id"],
                        occurred_at=message["received_at"],
                        company_time=self.company_time(),
                        document={
                            "message_id": message_id,
                            "subject": message["subject"],
                            "title": message["subject"],
                            "body": message["body"],
                            "from_actor_id": sender_id,
                            "to_actor_id": mailbox["actor_id"],
                            "related_document": {"doctype": mapping.erp_doctype, "name": mapping.erp_name},
                            "in_reply_to": in_reply_to,
                            "references": re.findall(r"<[^<>\s]+>", message.get("references", "")),
                            "mailbox_uid": message["mailbox_uid"],
                            "uidvalidity": message["uidvalidity"],
                        },
                        evidence_refs=[message["webmail_url"]],
                        message_id=message_id,
                        in_reply_to=in_reply_to,
                    )
                    self.pipeline.capture(evidence)
                    with self.store.transaction() as db:
                        db.execute(
                            "INSERT INTO company_coffee_mail_cursors(mapping_id,mailbox_actor_id,uidvalidity,after_uid,updated_at) "
                            "VALUES(?,?,?,?,?) ON CONFLICT(mapping_id,mailbox_actor_id) DO UPDATE SET "
                            "uidvalidity=excluded.uidvalidity,after_uid=MAX(company_coffee_mail_cursors.after_uid,excluded.after_uid),updated_at=excluded.updated_at",
                            (mapping.mapping_id, mailbox["actor_id"], message["uidvalidity"],
                             int(message["mailbox_uid"]), datetime.now(timezone.utc).isoformat()),
                        )
                    self.pipeline.observe(evidence)
            except Exception as error:
                failures.append({"mapping_id": mapping.mapping_id, "error_type": type(error).__name__})
                frames = [(frame.filename, frame.lineno, frame.name)
                          for frame in traceback.extract_tb(error.__traceback__)]
                causes = []
                cause = error.__cause__ or error.__context__
                while cause is not None:
                    causes.append(type(cause).__name__)
                    cause = cause.__cause__ or cause.__context__
                LOG.error(
                    "CoffeeBench mapped mail observation failed mapping_id=%s mailbox_actor_id=%s "
                    "exception_type=%s causes=%s stack=%s",
                    mapping.mapping_id, mailbox["actor_id"], type(error).__name__, causes, frames,
                )
        component = "component_coffee_company_mail"
        with self.store.transaction() as db:
            if failures:
                db.execute(
                    "INSERT INTO company_feedback_runtime_reports VALUES(?,?) "
                    "ON CONFLICT(provider) DO UPDATE SET result_json=excluded.result_json",
                    (component, json.dumps({"status": "partial", "failures": failures}, sort_keys=True)),
                )
            else:
                db.execute("DELETE FROM company_feedback_runtime_reports WHERE provider=?", (component,))
        self._observe_unmapped_coffee_conversations(gateway, profile, actor_by_id)

    def _observe_unmapped_coffee_conversations(self, gateway, profile, actor_by_id):
        """Continue exact native Coffee threads without inventing ERP records."""
        from .mail import MailReceiptMismatch
        from .company_feedback import encoded

        with self.store.transaction() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM company_native_message_outbox "
                "WHERE status='delivered' AND mapping_id IS NULL ORDER BY source_event_id"
            )]
        company_mail_actor_ids = set(_coffee_company_mail_actor_ids(profile, self.config.mail))
        failures = []
        for row in rows:
            try:
                payload = json.loads(row["private_payload_json"])
                native = payload.get("native_source_message")
                if not isinstance(native, dict):
                    # Pre-conversation outbox rows lack the native identity needed
                    # to safely route a reply. Keep this visible for operator repair.
                    raise MailReceiptMismatch("native source identity was not retained with delivered mail")
                source_event_id = row["source_event_id"]
                root_id = row["message_id"]
                if not all(isinstance(native.get(field), str) and native[field] for field in
                           ("native_message_id", "sender_id", "recipient_id")):
                    raise MailReceiptMismatch("native Coffee thread identity is incomplete")
                coffee = profile["company"]["background"]["coffee"]
                counterparty = coffee.get("counterparties", {}).get(native["sender_id"])
                if not isinstance(counterparty, dict):
                    raise MailReceiptMismatch("native Coffee thread has no configured counterparty identity")
                mailbox = actor_by_id.get(counterparty.get("actor_id"))
                if not mailbox or mailbox.get("kind") != "counterparty":
                    raise MailReceiptMismatch("native Coffee thread counterparty mailbox is unavailable")
                with self.store.transaction() as db:
                    cursor = db.execute(
                        "SELECT uidvalidity,after_uid FROM company_coffee_mail_cursors WHERE mapping_id=? AND mailbox_actor_id=?",
                        ("conversation:" + source_event_id, mailbox["actor_id"]),
                    ).fetchone()
                after_uid = int(cursor["after_uid"]) if cursor else 0
                uidvalidity = str(cursor["uidvalidity"]) if cursor else None
                replies = gateway.poll_registered_replies(
                    mailbox, {root_id}, after_uid, company_mail_actor_ids,
                    expected_uidvalidity=uidvalidity,
                    require_authenticated_submission=True,
                )
                for reply in replies:
                    if reply.get("authenticated_sender_actor_id") != reply.get("sender_actor_id"):
                        raise MailReceiptMismatch("Coffee company reply has no matching authenticated mailbox identity")
                    referenced_roots = {
                        reply.get("in_reply_to"),
                        *re.findall(r"<[^<>\s]+>", reply.get("references", "")),
                    }
                    if root_id not in referenced_roots:
                        continue
                    # A reply spanning multiple delivered native roots is not
                    # assigned to either source automatically.
                    other_roots = {
                        item["message_id"] for item in rows
                        if item["message_id"] in referenced_roots
                    }
                    if other_roots != {root_id}:
                        raise MailReceiptMismatch("Coffee reply references multiple registered native threads")
                    arguments = {
                        "recipient": native["sender_id"],
                        "title": reply["subject"][:80],
                        "body": reply["body"],
                    }
                    for key in ("ref_listing_id", "ref_offer_id"):
                        if isinstance(native.get(key), str) and native[key]:
                            arguments[key] = native[key]
                    incoming_id = reply["message_id"]
                    action_id = hashlib.sha256(
                        f"coffee-conversation:{source_event_id}:{incoming_id}:{native['sender_id']}".encode()
                    ).hexdigest()
                    action = TranslatedAction(
                        action_id=action_id,
                        # This scoped identifier is an action receipt key only;
                        # no RecordMapping or ERP document is created.
                        mapping_id="coffee-conversation:" + source_event_id,
                        evidence_id=identity("coffee-conversation-mail", incoming_id),
                        benchmark="coffeebench",
                        native_action="send_message",
                        actor_id=native["recipient_id"],
                        company_time=self.company_time(),
                        arguments=arguments,
                    ).model_dump(mode="json")
                    request_hash = hashlib.sha256(encoded(action).encode()).hexdigest()
                    thread_key = hashlib.sha256(
                        f"{source_event_id}:{native['sender_id']}:{native['recipient_id']}".encode()
                    ).hexdigest()
                    with self.store.transaction() as db:
                        existing = db.execute(
                            "SELECT * FROM company_coffee_thread_actions WHERE action_id=?",
                            (action_id,),
                        ).fetchone()
                        if existing is None:
                            db.execute(
                                "INSERT INTO company_coffee_thread_actions(action_id,source_event_id,incoming_message_id,thread_key,native_actor_id,native_recipient_id,native_refs_json,company_mail_actor_id,request_hash,action_json,mail_receipt_json,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                                (action_id, source_event_id, incoming_id, thread_key, native["recipient_id"],
                                 native["sender_id"], json.dumps({key: arguments[key] for key in ("ref_listing_id", "ref_offer_id") if key in arguments}, sort_keys=True),
                                 reply["sender_actor_id"], request_hash, encoded(action), encoded({key: reply[key] for key in (
                                     "message_id", "in_reply_to", "references", "received_at", "mailbox_uid", "uidvalidity", "reference", "authenticated_sender_actor_id")}),
                                 datetime.now(timezone.utc).isoformat()),
                            )
                        else:
                            action = json.loads(existing["action_json"])
                            if existing["incoming_message_id"] != incoming_id:
                                raise MailReceiptMismatch("Coffee conversation action identity conflicts with stored receipt")
                            stored_args = action.get("arguments", {})
                            if any(stored_args.get(key) != arguments.get(key) for key in arguments):
                                raise MailReceiptMismatch("Coffee conversation Message-ID was reused with changed business content")
                            # Retries use the immutable originally claimed action,
                            # including its company-clock timestamp.
                            request_hash = existing["request_hash"]
                    adapter = self.pipeline.adapters["coffeebench"]
                    try:
                        receipt = adapter.reconcile(action)
                        if receipt is None:
                            with self.store.transaction() as db:
                                db.execute("UPDATE company_coffee_thread_actions SET status='applying',updated_at=? WHERE action_id=? AND status IN ('pending','uncertain')",
                                           (datetime.now(timezone.utc).isoformat(), action_id))
                            receipt = adapter.apply(action)
                    except Exception as error:
                        with self.store.transaction() as db:
                            db.execute("UPDATE company_coffee_thread_actions SET status='uncertain',error_type=?,updated_at=? WHERE action_id=?",
                                       (type(error).__name__, datetime.now(timezone.utc).isoformat(), action_id))
                        raise
                    if receipt.action_id != action_id:
                        raise MailReceiptMismatch("Coffee worker returned a different conversation action receipt")
                    with self.store.transaction() as db:
                        db.execute("UPDATE company_coffee_thread_actions SET status=?,receipt_json=?,error_type=NULL,updated_at=? WHERE action_id=?",
                                   ("applied" if receipt.accepted and receipt.reconciled else "rejected",
                                    encoded(receipt.model_dump(mode="json")), datetime.now(timezone.utc).isoformat(), action_id))
                        db.execute(
                            "INSERT INTO company_coffee_mail_cursors(mapping_id,mailbox_actor_id,uidvalidity,after_uid,updated_at) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(mapping_id,mailbox_actor_id) DO UPDATE SET uidvalidity=excluded.uidvalidity,after_uid=MAX(company_coffee_mail_cursors.after_uid,excluded.after_uid),updated_at=excluded.updated_at",
                            ("conversation:" + source_event_id, mailbox["actor_id"], reply["uidvalidity"], int(reply["mailbox_uid"]), datetime.now(timezone.utc).isoformat()),
                        )
            except Exception as error:
                failures.append({"source_event_id": row["source_event_id"], "error_type": type(error).__name__})
                frames = [(frame.filename, frame.lineno, frame.name) for frame in traceback.extract_tb(error.__traceback__)]
                LOG.error("Coffee native conversation observation failed source_event_id=%s exception_type=%s stack=%s",
                          row["source_event_id"], type(error).__name__, frames)
        component = "component_coffee_conversation_mail"
        with self.store.transaction() as db:
            if failures:
                db.execute("INSERT INTO company_feedback_runtime_reports VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET result_json=excluded.result_json",
                           (component, json.dumps({"status": "partial", "failures": failures}, sort_keys=True)))
            else:
                db.execute("DELETE FROM company_feedback_runtime_reports WHERE provider=?", (component,))

    def _observe_issue_mail(self):
        from email.utils import parseaddr

        with self.store.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_issue_handoffs'"
            ).fetchone():
                return
            tracked = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM company_issue_handoffs WHERE delivered_at IS NOT NULL"
                )
            ]
        if not tracked:
            return
        from .mail import MailuGateway
        from .runtime import private_json

        gateway = MailuGateway(
            self.config.mail, private_json(self.config.mail_credentials_file)
        )
        actors = {
            actor["email"].casefold(): actor for actor in self.config.mail["actors"]
        }
        for row in tracked:
            notification = json.loads(row["notification_json"])
            actor = actors[notification["recipient_email"].casefold()]
            known_ids = {notification["message_id"]}
            if row["handoff_json"]:
                known_ids.add(json.loads(row["handoff_json"])["message_id"])
            messages = gateway.poll(
                actor, 0, "company-issue:" + row["issue_key"], 0, known_ids
            )
            for message in messages:
                message_id = message["message_id"]
                if message_id in known_ids or self._mail_evidence_exists(message_id):
                    continue
                # RFC From is a claim, not authenticated company action authority.
                claimed_sender = parseaddr(message.get("from_header", ""))[1]
                evidence = CompanyEvidence(
                    evidence_id=identity("mail", message_id),
                    erp_doctype="Communication",
                    erp_name=message_id,
                    event="email_reply"
                    if message.get("in_reply_to")
                    else "email_delivered",
                    actor=claimed_sender or "unidentified-email-sender",
                    recipient_actor_id=actor["actor_id"],
                    occurred_at=datetime.now(timezone.utc),
                    company_time=self.company_time(),
                    document=message,
                    evidence_refs=[message["webmail_url"]],
                    message_id=message_id,
                    in_reply_to=message.get("in_reply_to"),
                )
                self.pipeline.observe(evidence)
            if row["stage"] == "sent" and row["handoff_json"]:
                intent = json.loads(row["handoff_json"])
                if self._mail_evidence_exists(intent["message_id"], "sent-mail"):
                    continue
                evidence = CompanyEvidence(
                    evidence_id=identity("sent-mail", intent["message_id"]),
                    erp_doctype="Communication",
                    erp_name=intent["message_id"],
                    event="email_reply",
                    actor=intent["sender"]["actor_id"],
                    authenticated_sender_actor_id=intent["sender"]["actor_id"],
                    recipient_actor_id="viv",
                    occurred_at=row["updated_at"],
                    company_time=self.company_time(),
                    document={"subject": intent["subject"], "body": intent["body"]},
                    evidence_refs=["issue-handoff:" + row["issue_key"]],
                    message_id=intent["message_id"],
                    in_reply_to=notification["message_id"],
                )
                self.pipeline.observe(evidence)

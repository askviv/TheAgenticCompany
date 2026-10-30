"""Durable coordinator lifecycle for company-scoped native τ provider cases."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import uuid
import logging
import re
import traceback
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field

from .provider_contracts import (
    ProviderCasePrepareResponse, ProviderPortalCaseRegistration, ProviderTaskCatalog, ProviderId,
)
from .provider_portal_client import ProviderPortalClient
from .store import SQLiteStore, StoreConflict


_NAMESPACE = uuid.UUID("0dbbb83d-e572-44a3-a57b-b994d64413c9")
LOG = logging.getLogger(__name__)


def _log_failure(operation: str, experiment_id: str, error: BaseException) -> None:
    frames = [(frame.filename, frame.lineno, frame.name)
              for frame in traceback.extract_tb(error.__traceback__)]
    causes = []
    cause = error.__cause__ or error.__context__
    while cause is not None:
        causes.append(type(cause).__name__)
        cause = cause.__cause__ or cause.__context__
    LOG.error("provider experiment operation failed operation=%s experiment_id=%s exception_type=%s causes=%s stack=%s",
              operation, experiment_id, type(error).__name__, causes, frames)


class ProviderExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider_id: ProviderId
    task_id: str = Field(min_length=1, max_length=240)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_code(value: Any, default: str) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,79}", value) else default


class ProviderExperimentStore:
    """Provider case status shares the coordinator's existing durable SQLite DB."""

    def __init__(self, store: SQLiteStore):
        self.store = store
        with store.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS company_provider_experiments (
                experiment_id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                upstream_commit TEXT,
                conversation_id TEXT NOT NULL UNIQUE,
                caller_actor_id TEXT NOT NULL,
                idempotency_key_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'preparing','initialization_uncertain','portal_registration_pending','portal_registration_uncertain',
                    'registered','failed')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                preparation_json TEXT,
                case_json TEXT,
                failure_code TEXT,
                failure_type TEXT,
                UNIQUE(company_id,caller_actor_id,idempotency_key_hash)
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(company_provider_experiments)")}
            if "upstream_commit" not in columns:
                db.execute("ALTER TABLE company_provider_experiments ADD COLUMN upstream_commit TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS company_provider_experiment_events (
                experiment_id TEXT NOT NULL REFERENCES company_provider_experiments(experiment_id),
                sequence INTEGER NOT NULL,
                status TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                detail_code TEXT,
                PRIMARY KEY(experiment_id,sequence)
            )""")

    def reserve(self, request: ProviderExperimentRequest, actor_id: str, key: str) -> tuple[dict, bool]:
        if not actor_id or not key or len(key) > 240:
            raise ValueError("provider experiment caller and idempotency key are required")
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        request_json = _canonical(request.model_dump(mode="json"))
        request_hash = hashlib.sha256(request_json.encode()).hexdigest()
        experiment_id = str(uuid.uuid5(_NAMESPACE, f"company:{actor_id}:{key_hash}"))
        created = _now()
        with self.store.transaction() as db:
            prior = db.execute(
                "SELECT * FROM company_provider_experiments WHERE company_id='company' "
                "AND caller_actor_id=? AND idempotency_key_hash=?", (actor_id, key_hash),
            ).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise StoreConflict("provider experiment idempotency key was reused for another request")
                return dict(prior), False
            db.execute(
                "INSERT INTO company_provider_experiments(experiment_id,company_id,provider_id,task_id,"
                "conversation_id,caller_actor_id,idempotency_key_hash,request_hash,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (experiment_id, "company", request.provider_id, request.task_id, experiment_id,
                 actor_id, key_hash, request_hash, "preparing", created, created),
            )
            # EvaluationStore's reservation ledger is keyed by its established
            # run identity. This hidden registered subject is the actual τ
            # task/case, not a simulated scenario run and is never active in
            # the scenario pump.
            manifest = _canonical({
                "source_kind": "company_provider",
                "company_id": "company",
                "provider_experiment_id": experiment_id,
                "provider_id": request.provider_id,
                "task_id": request.task_id,
            })
            db.execute(
                "INSERT INTO runs(run_id,scenario_id,generation,seed,agent_id,status,simulated_time,paused,"
                "manifest_json,erp_binding_json) VALUES(?, 'TAU_PROVIDER', 0, 0, ?, 'registered', NULL, 0, ?, '{}')",
                (experiment_id, "company-provider-case", manifest),
            )
            db.execute(
                "INSERT INTO company_provider_experiment_events VALUES(?,1,'preparing',?,NULL)",
                (experiment_id, created),
            )
            row = db.execute("SELECT * FROM company_provider_experiments WHERE experiment_id=?",
                             (experiment_id,)).fetchone()
        return dict(row), True

    def get(self, experiment_id: str) -> dict | None:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM company_provider_experiments WHERE experiment_id=?",
                             (experiment_id,)).fetchone()
            if row is None:
                return None
            value = dict(row)
            value["events"] = [dict(event) for event in db.execute(
                "SELECT status,occurred_at,detail_code FROM company_provider_experiment_events "
                "WHERE experiment_id=? ORDER BY sequence", (experiment_id,))]
        return value

    def list(self) -> list[dict]:
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT experiment_id,company_id,provider_id,task_id,upstream_commit,conversation_id,status,created_at,updated_at "
                "FROM company_provider_experiments WHERE company_id='company' ORDER BY created_at DESC,experiment_id"
            )
            return [dict(row) for row in rows]

    def set_upstream_commit(self, experiment_id: str, upstream_commit: str) -> dict:
        with self.store.transaction() as db:
            db.execute("UPDATE company_provider_experiments SET upstream_commit=?,updated_at=? "
                       "WHERE experiment_id=? AND (upstream_commit IS NULL OR upstream_commit=?)",
                       (upstream_commit, _now(), experiment_id, upstream_commit))
            row = db.execute("SELECT * FROM company_provider_experiments WHERE experiment_id=?",
                             (experiment_id,)).fetchone()
            if row is None:
                raise KeyError(experiment_id)
            if row["upstream_commit"] != upstream_commit:
                raise StoreConflict("provider experiment upstream revision changed")
        return dict(row)

    def update(self, experiment_id: str, status: str, *, preparation: dict | None = None,
               case: dict | None = None, failure_code: str | None = None,
               failure_type: str | None = None) -> dict:
        allowed = {"preparing", "initialization_uncertain", "portal_registration_pending",
                   "portal_registration_uncertain", "registered", "failed"}
        if status not in allowed:
            raise ValueError("provider experiment status is invalid")
        now = _now()
        with self.store.transaction() as db:
            row = db.execute("SELECT status FROM company_provider_experiments WHERE experiment_id=?",
                             (experiment_id,)).fetchone()
            if row is None:
                raise KeyError(experiment_id)
            current = row["status"]
            if current == "registered" and status != "registered":
                raise StoreConflict("registered provider experiment cannot regress")
            sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM company_provider_experiment_events WHERE experiment_id=?",
                (experiment_id,),
            ).fetchone()[0]
            if current != status or failure_code:
                db.execute("INSERT INTO company_provider_experiment_events VALUES(?,?,?,?,?)",
                           (experiment_id, sequence, status, now, failure_code))
            db.execute(
                "UPDATE company_provider_experiments SET status=?,updated_at=?,"
                "preparation_json=COALESCE(?,preparation_json),case_json=COALESCE(?,case_json),"
                "failure_code=?,failure_type=? WHERE experiment_id=?",
                (status, now, _canonical(preparation) if preparation is not None else None,
                 _canonical(case) if case is not None else None, failure_code, failure_type, experiment_id),
            )
        result = self.get(experiment_id)
        assert result is not None
        return result


class ProviderExperimentService:
    def __init__(self, store: SQLiteStore, worker: Any, portal: ProviderPortalClient,
                 responsible_employee: dict[str, str], *, portal_url: str,
                 experiment_store: ProviderExperimentStore | None = None, now=_now):
        self.store = experiment_store or ProviderExperimentStore(store)
        self.worker = worker
        self.portal = portal
        self.employee = dict(responsible_employee)
        self.portal_url = portal_url.rstrip("/") + "/"
        self.now = now
        # The portal and coordinator use the same durable SQLite database.
        # Read its allowlisted case activity directly; never read mail/native
        # response JSON from the portal tables here.
        from .provider_portal_store import ProviderPortalStore
        self.portal_store = ProviderPortalStore(store)

    def catalog(self) -> dict:
        result = ProviderTaskCatalog.model_validate(self.worker.catalog())
        return result.model_dump(mode="json")

    def list(self) -> list[dict]:
        return [self.public(row) for row in self.store.list()]

    def get(self, experiment_id: str) -> dict:
        row = self.store.get(experiment_id)
        if row is None:
            raise KeyError(experiment_id)
        return self.public(row)

    def company_dashboard(self) -> tuple[list[dict], list[dict]]:
        """Return provider cases in the company dashboard's public shape.

        These cases intentionally have hidden evaluation subjects and are not
        sent through ordinary run iteration, event pumping, or run controls.
        """
        experiments: list[dict] = []
        activity: list[dict] = []
        public_status = {
            "preparing": "initializing",
            "initialization_uncertain": "uncertain",
            "portal_registration_pending": "initializing",
            "portal_registration_uncertain": "uncertain",
            "registered": "running",
            "failed": "failed",
        }
        for row in self.store.list():
            experiment_id = row["experiment_id"]
            experiments.append({
                "experiment_id": experiment_id,
                "scenario_id": "TAU_PROVIDER",
                "generation": 0,
                "agent_id": None,
                "status": public_status[row["status"]],
                "simulated_time": None,
                "paused": False,
                "kind": "provider_case",
                "provider_id": row["provider_id"],
                "task_id": row["task_id"],
            })
            case_activity = self.portal_store.company_case_activity(experiment_id, self.portal_url)
            if case_activity is not None:
                activity.extend(self._public_email_activity(experiment_id, case_activity))
            stored = self.store.get(experiment_id)
            if stored is None:
                continue
            activity.extend(self._public_lifecycle_activity(experiment_id, stored))
        experiments.sort(key=lambda item: item["experiment_id"])
        return experiments, activity

    @staticmethod
    def _email_event(message: dict) -> str:
        direction = message.get("direction")
        status = message.get("status")
        if direction == "inbound" and message.get("received_at"):
            return "email_received"
        if direction == "outbound" and status == "sent":
            return "email_delivered"
        if status in {"pending", "started", "outbound_submitting", "reply_submitting"}:
            return "email_pending"
        return "email_uncertain"

    @classmethod
    def _public_email_activity(cls, experiment_id: str, case: dict) -> list[dict]:
        descriptions = {
            "email_delivered": "A company email was delivered.",
            "email_received": "A company email reply was received.",
            "email_pending": "A company email is being processed.",
            "email_uncertain": "A company email is awaiting delivery confirmation.",
        }
        result = []
        for message in case.get("activity", []):
            event = cls._email_event(message)
            received_at = message.get("received_at")
            created_at = message.get("created_at")
            result.append({
                "id": f"provider-email:{experiment_id}:{message['message_id']}",
                "timestamp": received_at or created_at,
                "created_at": created_at,
                "received_at": received_at,
                "event": event,
                "description": descriptions[event],
                "type": "email",
                "experiment_id": experiment_id,
                "scenario_id": "TAU_PROVIDER",
                "title": case.get("subject", "Company provider case"),
                "summary": descriptions[event],
                "content": message.get("body", ""),
                "direction": "sent" if message.get("direction") == "outbound" else "received",
            })
        return result

    @staticmethod
    def _public_lifecycle_activity(experiment_id: str, row: dict) -> list[dict]:
        descriptions = {
            "preparing": "The company provider case is being prepared.",
            "initialization_uncertain": "Provider setup is awaiting confirmation.",
            "portal_registration_pending": "The employee request is being delivered.",
            "portal_registration_uncertain": "The employee request is awaiting delivery confirmation.",
            "registered": "The company request was delivered.",
            "failed": "The provider case could not be initialized.",
        }
        return [{
            "id": f"provider-status:{experiment_id}:{index}",
            "timestamp": event["occurred_at"],
            "event": event["status"],
            "description": descriptions[event["status"]],
            "experiment_id": experiment_id,
            "scenario_id": "TAU_PROVIDER",
        } for index, event in enumerate(row.get("events", []))]

    def launch(self, raw_request: dict, actor_id: str, idempotency_key: str) -> dict:
        request = ProviderExperimentRequest.model_validate(raw_request)
        row, _created = self.store.reserve(request, actor_id, idempotency_key)
        if row["status"] in {"registered", "failed"}:
            return self.public(row)
        try:
            catalog = self.catalog()
            catalog_task = next((task for task in catalog["tasks"]
                                 if task.get("provider_id") == row["provider_id"]
                                 and task.get("task_id") == row["task_id"]), None)
            if catalog_task is None:
                row = self.store.update(row["experiment_id"], "failed",
                                        failure_code="provider_task_not_found", failure_type="ProviderTaskNotFound")
                return self.public(row)
            commit = catalog.get("upstream_commit")
            if not isinstance(commit, str) or len(commit) != 40:
                raise ValueError("native provider catalogue omitted its pinned source revision")
            row = self.store.set_upstream_commit(row["experiment_id"], commit)
        except Exception as error:
            _log_failure("catalogue", row["experiment_id"], error)
            row = self.store.update(row["experiment_id"], "preparing",
                                    failure_code=("provider_source_revision_mismatch"
                                                  if isinstance(error, StoreConflict)
                                                  else _safe_code(getattr(error, "code", None),
                                                                  "provider_catalogue_unavailable")),
                                    failure_type=type(error).__name__)
            return self.public(row)
        if row.get("case_json") is not None:
            case_data = json.loads(row["case_json"])
        else:
            try:
                prepared = self.worker.prepare({
                    "provider_id": row["provider_id"], "task_id": row["task_id"],
                    "conversation_id": row["conversation_id"],
                    "company_actor_id": self.employee["actor_id"],
                    "company_account_reference": f"company-account:{row['experiment_id']}",
                    "company_issue_reference": f"company-issue:{row['experiment_id']}",
                })
            except Exception as error:
                _log_failure("prepare", row["experiment_id"], error)
                error_code = getattr(error, "code", None)
                status_code = getattr(error, "status_code", None)
                uncertain = (getattr(error, "uncertain", False)
                             or (isinstance(error_code, str) and error_code.endswith("uncertain"))
                             or status_code is None
                             or (isinstance(status_code, int) and status_code >= 500))
                status = "initialization_uncertain" if uncertain else "failed"
                row = self.store.update(row["experiment_id"], status,
                                        failure_code=_safe_code(getattr(error, "code", None),
                                                                "provider_prepare_failed"),
                                        failure_type=type(error).__name__)
                return self.public(row)
            try:
                prepared = ProviderCasePrepareResponse.model_validate(prepared).model_dump(mode="json")
                if prepared.get("status") == "conflict" or prepared.get("initialization_status") == "conflict":
                    row = self.store.update(row["experiment_id"], "failed", preparation=prepared,
                                            failure_code="native_initialization_conflict",
                                            failure_type="ProviderPrepareConflict")
                    return self.public(row)
                if prepared.get("status") not in {"prepared", "already_prepared"}:
                    raise ValueError("native provider preparation returned an unsupported status")
                if prepared.get("initialization_status") not in {"applied", "already_applied"}:
                    raise ValueError("native provider initialization returned an unsupported status")
                case_data = self._case_registration(row, prepared)
            except (TypeError, ValueError) as error:
                _log_failure("prepare_response", row["experiment_id"], error)
                row = self.store.update(row["experiment_id"], "failed", preparation=prepared,
                                        failure_code="invalid_native_prepare_response",
                                        failure_type=type(error).__name__)
                return self.public(row)
            row = self.store.update(row["experiment_id"], "portal_registration_pending",
                                    preparation=prepared, case=case_data)
        try:
            self.portal.register_case(case_data)
        except Exception as error:
            _log_failure("portal_registration", row["experiment_id"], error)
            error_code = getattr(error, "code", None)
            status_code = getattr(error, "status_code", None)
            uncertain = ((isinstance(error_code, str) and error_code.endswith("uncertain"))
                         or status_code is None
                         or (isinstance(status_code, int) and status_code >= 500))
            if not uncertain:
                row = self.store.update(row["experiment_id"], "failed",
                                        failure_code=_safe_code(getattr(error, "code", None),
                                                                "portal_registration_failed"),
                                        failure_type=type(error).__name__)
                return self.public(row)
            row = self.store.update(row["experiment_id"], "portal_registration_uncertain",
                                    failure_code=_safe_code(getattr(error, "code", None),
                                                            "portal_registration_failed"),
                                    failure_type=type(error).__name__)
            return self.public(row)
        row = self.store.update(row["experiment_id"], "registered", failure_code=None, failure_type=None)
        return self.public(row)

    def _case_registration(self, row: dict, prepared: dict) -> dict:
        expected = ("subject", "business_description", "requested_outcome")
        if any(not isinstance(prepared.get(field), str) or not prepared[field] for field in expected):
            raise ValueError("native provider preparation omitted safe case details")
        expected_refs = {
            "provider_id": row["provider_id"], "task_id": row["task_id"],
            "conversation_id": row["conversation_id"],
            "company_account_reference": f"company-account:{row['experiment_id']}",
            "company_issue_reference": f"company-issue:{row['experiment_id']}",
        }
        if any(prepared.get(field) != value for field, value in expected_refs.items()):
            raise ValueError("native provider preparation changed the durable case identity")
        employee = self.employee
        result = ProviderPortalCaseRegistration(
            case_id=row["experiment_id"], provider_id=row["provider_id"], task_id=row["task_id"],
            conversation_id=row["conversation_id"], responsible_actor_id=employee["actor_id"],
            responsible_email=employee["email"],
            company_account_reference=f"company-account:{row['experiment_id']}",
            issue_reference=f"company-issue:{row['experiment_id']}",
            subject=prepared["subject"], business_description=prepared["business_description"],
            requested_outcome=prepared["requested_outcome"],
            private_factual_context=prepared.get("private_factual_context", []),
            created_at=row["created_at"],
        )
        return result.model_dump(mode="json")

    def public(self, row: dict) -> dict:
        result = {key: row[key] for key in (
            "experiment_id", "company_id", "provider_id", "task_id", "upstream_commit", "conversation_id",
            "status", "created_at", "updated_at", "failure_code", "failure_type",
        ) if key in row and row[key] is not None}
        if row.get("preparation_json"):
            prepared = json.loads(row["preparation_json"])
            result["initialization_status"] = prepared.get("initialization_status")
            result["initialization_revision"] = prepared.get("initialization_revision")
            result["account"] = prepared.get("account")
            result["forms"] = prepared.get("forms", [])
            if prepared.get("upstream_commit"):
                result["upstream_commit"] = prepared["upstream_commit"]
        if row.get("case_json"):
            case = json.loads(row["case_json"])
            result["subject"] = case.get("subject")
            result["business_description"] = case.get("business_description")
            result["requested_outcome"] = case.get("requested_outcome")
            result["portal_url"] = self.portal_url + "?case=" + quote(row["experiment_id"], safe="")
        result["handoff_email_status"] = (
            "sent" if row.get("status") == "registered"
            else "uncertain" if row.get("status") == "portal_registration_uncertain"
            else "pending"
        )
        if "events" in row:
            activity = [{key: event[key] for key in ("status", "occurred_at", "detail_code")
                         if event.get(key) is not None} for event in row["events"]]
            case_activity = self.portal_store.company_case_activity(row["experiment_id"], self.portal_url)
            if case_activity is not None:
                for message in self._public_email_activity(row["experiment_id"], case_activity):
                    activity.append({
                        "status": message["event"],
                        "occurred_at": message["timestamp"],
                        "created_at": message["created_at"],
                        "received_at": message["received_at"],
                        "message_id": message["id"],
                        "direction": message["direction"],
                        "body": message["content"],
                        "subject": message["title"],
                    })
            activity.sort(key=lambda item: (str(item.get("occurred_at", "")),
                                            str(item.get("message_id", item.get("status", "")))))
            result["activity"] = activity
        return result

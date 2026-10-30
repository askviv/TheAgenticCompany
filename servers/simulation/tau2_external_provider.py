"""τ native external-provider adapter using the upstream agent, tools and evaluators."""

from __future__ import annotations

import hashlib
import re
import json
import os
from contextlib import nullcontext
from pathlib import Path
import tomllib
from typing import Any, Callable
from copy import deepcopy
from decimal import Decimal
from functools import lru_cache

from pydantic import TypeAdapter

from servers.simulation.provider_contracts import (
    CompanyPaymentEvidence, CompanyProviderFinancialAction, CompanyProviderPortalAction,
    NativeProviderReference, ProviderBankStateEvidence, ProviderBillEventEvidence, ProviderEvaluationResult,
    ProviderBankActionReceipt, ProviderPaymentReceipt, ProviderRequest, ProviderResponse,
    ProviderTaskCatalog, ProviderTaskCatalogEntry, ProviderBusinessAccount,
    ProviderBusinessRecord, ProviderPortalForm, ProviderCasePrepareRequest,
    ProviderCasePrepareResponse,
    ProviderEvaluationSnapshotReceipt, ProviderEvaluationSnapshotCheck,
    ProviderEvaluationSnapshotVerification,
)
from servers.simulation.provider_store import NativeProviderStore, native_record_digests
from servers.simulation.actor_worker_store import canonical_request_hash

TAU2_SOURCE_COMMIT = "7aad81c37a3a78b4ed3e4866769f851400c64415"
PROVIDER_LLM = "tac_codex/codex-cli"
PROVIDER_DOMAIN_FILES = {
    "tau2-airline": ("airline", "tasks.json"),
    "tau2-telecom": ("telecom", "tasks.json"),
    "tau2-banking": ("banking_knowledge", "tasks"),
}
class ExternalProviderError(RuntimeError):
    pass


class ProviderTaskNotFound(ExternalProviderError):
    pass


def _banking_patch_conflicts(current_db: dict[str, Any], baseline_db: dict[str, Any],
                             initialization_data: dict[str, Any] | None) -> list[str]:
    """Find conflicting task patch tables while allowing untouched base values."""
    agent_patch = (initialization_data or {}).get("agent_data") or {}
    conflicts: set[str] = set()
    missing = object()

    def visit(current: Any, baseline: Any, patch: Any, path: tuple[str, ...]) -> None:
        # A source patch adds a previously absent entity row. If another company
        # case has already claimed that same source ID with different content,
        # do not merge two competing entity records field by field.
        if len(path) >= 3 and baseline is missing and current is not missing and current != patch:
            conflicts.add(path[0] if path else "unknown")
            return
        if isinstance(patch, dict):
            if current is not missing and not isinstance(current, dict):
                if current != baseline and current != patch:
                    conflicts.add(path[0] if path else "unknown")
                    return
            if current == patch:
                return
            current_map = current if isinstance(current, dict) else {}
            baseline_map = baseline if isinstance(baseline, dict) else {}
            for key, value in patch.items():
                visit(current_map.get(key, missing), baseline_map.get(key, missing),
                      value, (*path, str(key)))
            return
        if current != baseline and current != patch:
            conflicts.add(path[0] if path else "unknown")

    for table_name, table_patch in agent_patch.items():
        if table_name == "task_config":
            continue
        visit(current_db.get(table_name, missing), baseline_db.get(table_name, missing),
              table_patch, (table_name,))
    return sorted(conflicts)


def _banking_task_config_patch(initialization_data) -> dict[str, Any] | None:
    if initialization_data is None or initialization_data.agent_data is None:
        return None
    return (initialization_data.agent_data.get("task_config")
            if isinstance(initialization_data.agent_data, dict) else None)


def _banking_read_log_allowlist(task) -> set[str]:
    """Resolve discoverable read tool names from the unchanged native trajectory."""
    names: set[str] = set()
    criteria = task.evaluation_criteria
    for action in criteria.actions if criteria else []:
        if action.requestor != "assistant":
            continue
        name = action.arguments.get("agent_tool_name") if action.name == "call_discoverable_agent_tool" else action.name
        if name:
            names.add(name)
    return names


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _task_native_record_refs(task, agent_db: dict[str, Any], user_db: dict[str, Any] | None) -> set[str]:
    """Find exact native records named by task setup or DB criteria.

    This covers read-only criteria as well as gold trajectories that mutate
    records. Only identifier strings are compared; task prose is never used as
    attribution input.
    """
    refs: set[str] = set()
    initial = task.initial_state
    initialization = getattr(initial, "initialization_data", None) if initial else None
    if initialization is not None:
        data = initialization.model_dump(mode="json") if hasattr(initialization, "model_dump") else initialization
        if isinstance(data, dict):
            refs.update(native_record_digests(data.get("agent_data") or {}, data.get("user_data")))

    identifiers: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, str):
            identifiers.add(value)

    criteria = task.evaluation_criteria
    if criteria is not None:
        for action in criteria.actions or []:
            collect(action.arguments)
        for assertion in criteria.env_assertions or []:
            collect(assertion.arguments)
    available = native_record_digests(agent_db, user_db)
    refs.update(ref for ref in available if ref.rsplit("/", 1)[-1] in identifiers)
    return refs


def _tree_hash(root: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest(), len(files)


def _private_json(value: Any) -> dict[str, Any]:
    result = value.model_dump(mode="json")
    result.pop("raw_data", None)
    for message in result.get("system_messages", []):
        message.pop("raw_data", None)
    for message in result.get("messages", []):
        message.pop("raw_data", None)
    return result


class Tau2ExternalProvider:
    """Run company messages through one persistent provider database per τ domain."""

    def __init__(self, store: NativeProviderStore, *, data_root: str | Path | None = None,
                 llm_alias: str = PROVIDER_LLM, agent_factory: Callable[..., Any] | None = None):
        self.store = store
        self.runtime_root = Path(__file__).with_name("tau2_actor_runtime")
        self.data_root = Path(data_root or self.runtime_root / "data").resolve()
        self.llm_alias = llm_alias
        self.agent_factory = agent_factory
        os.environ["TAU2_DATA_DIR"] = str(self.data_root)
        self._verify_assets()

    def _verify_assets(self) -> dict[str, Any]:
        path = self.runtime_root / "data/tau2/domains/external_provider_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("upstream_commit") != TAU2_SOURCE_COMMIT:
            raise ValueError("native provider asset manifest uses a different τ source revision")
        for entry in manifest["assets"]:
            source = self.runtime_root / entry["path"]
            if "tree_sha256" in entry:
                actual, file_count = _tree_hash(source)
                if actual != entry["tree_sha256"] or file_count != entry["file_count"]:
                    raise ValueError(f"native provider source tree hash mismatch: {entry['path']}")
                continue
            if hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError(f"native provider source asset hash mismatch: {entry['path']}")
        return manifest

    @staticmethod
    def _domain(provider_id: str) -> str:
        try:
            return PROVIDER_DOMAIN_FILES[provider_id][0]
        except KeyError as exc:
            raise ValueError("unsupported native τ external provider") from exc

    def _load_task(self, provider_id: str, task_id: str):
        from tau2.data_model.tasks import Task

        domain, task_file = PROVIDER_DOMAIN_FILES[provider_id]
        if domain == "banking_knowledge":
            from tau2.domains.banking_knowledge.environment import get_tasks

            for task in get_tasks(task_split_name=None):
                if task.id == task_id:
                    return task
            raise ProviderTaskNotFound("task is not in the pinned native provider source")
        path = self.runtime_root / f"data/tau2/domains/{domain}/{task_file}"
        rows = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(rows, dict):
            rows = rows.get("tasks", [])
        for row in rows:
            if str(row.get("id")) == task_id:
                return Task.model_validate(row)
        raise ProviderTaskNotFound("task is not in the pinned native provider source")

    @lru_cache(maxsize=8)
    def public_task_catalog(self) -> ProviderTaskCatalog:
        """Return task IDs with customer request summaries, never evaluator descriptions.

        τ's native ``description`` field contains evaluator purpose, including
        refusal conditions and hidden task intent. It is private benchmark data,
        so public catalog entries use only a structured customer request field
        or an explicit context-coverage note when no such field exists.
        """
        from tau2.data_model.tasks import Task

        rows = []
        for provider_id, (domain, task_file) in PROVIDER_DOMAIN_FILES.items():
            if domain == "airline":
                from tau2.domains.airline.environment import get_tasks

                tasks = get_tasks(task_split_name=None)
            elif domain == "banking_knowledge":
                from tau2.domains.banking_knowledge.environment import get_tasks

                tasks = get_tasks(task_split_name=None)
            else:
                path = self.runtime_root / f"data/tau2/domains/{domain}/{task_file}"
                data = json.loads(path.read_text(encoding="utf-8"))
                raw_tasks = data.get("tasks", []) if isinstance(data, dict) else data
                tasks = [Task.model_validate(row) for row in raw_tasks]
            for task in tasks:
                if provider_id == "tau2-banking":
                    from .tau2_banking_briefs import catalog_entry

                    rows.append(catalog_entry(str(task.id)))
                else:
                    rows.append(ProviderTaskCatalogEntry(
                        provider_id=provider_id,
                        task_id=str(task.id),
                        description=self._public_business_request(provider_id, task),
                        classification=self._public_task_classification(provider_id, task),
                    ))
        return ProviderTaskCatalog(upstream_commit=TAU2_SOURCE_COMMIT, tasks=rows)

    @staticmethod
    def _public_task_classification(provider_id: str, task) -> str | None:
        """Classify Telecom tasks from their unchanged native reference writes."""
        if provider_id != "tau2-telecom":
            return None
        criteria = task.evaluation_criteria
        actions = criteria.actions if criteria else []
        bill_actions = {"refuel_data", "send_payment_request", "make_payment"}
        return "financial" if any(action.name in bill_actions for action in actions or []) else "operational"

    @staticmethod
    def _public_business_request(provider_id: str, task) -> str:
        instructions = task.user_scenario.instructions
        if provider_id in {"tau2-airline", "tau2-telecom"}:
            # These domains have a specific initial request field separate from
            # task instructions and evaluator Purpose. The same request is what
            # the employee receives when a case is opened.
            request = getattr(instructions, "reason_for_call", None)
            if isinstance(request, str) and request.strip():
                return request.strip()
        if provider_id == "tau2-banking":
            # Banking stores no separate customer-facing request field. Its
            # only prose is a mixed user-model prompt, which is intentionally
            # kept out of the public catalog. Task.user_tools describes
            # capabilities, not the customer's request.
            return "Banking case; request context is held in private source instructions."
        return "External provider customer support request."

    @staticmethod
    def _banking_catalog_entry(task_id: str) -> ProviderTaskCatalogEntry:
        from .tau2_banking_briefs import catalog_entry

        return catalog_entry(task_id)

    @staticmethod
    def _banking_private_facts(provider_id: str, task_id: str):
        if provider_id != "tau2-banking":
            return []
        from .tau2_banking_briefs import private_factual_context

        return private_factual_context(task_id)

    @staticmethod
    def _case_business_context(provider_id: str, task, account: ProviderBusinessAccount | None) -> str:
        """Build case context only from upstream customer-facing fields and native state."""
        request = Tau2ExternalProvider._public_business_request(provider_id, task)
        if provider_id in {"tau2-airline", "tau2-telecom"}:
            known = getattr(task.user_scenario.instructions, "known_info", None)
            if isinstance(known, str) and known.strip():
                return request + "\n\nCustomer-supplied details: " + known.strip()
            return request
        if provider_id == "tau2-banking":
            from .tau2_banking_briefs import catalog_entry

            return catalog_entry(str(task.id)).description
        return request

    @staticmethod
    def _native_portal_forms(task, environment) -> list[ProviderPortalForm]:
        """Build only forms authorized by τ's native customer-tool declarations."""
        if getattr(environment, "user_tools", None) is None:
            return []
        forms = []
        declared_user_tools = task.user_tools
        native_tools = environment.get_user_tools(include=declared_user_tools)
        for tool in native_tools:
            if tool.name in {"call_discoverable_user_tool", "list_discoverable_user_tools"}:
                continue
            params = tool.params.model_json_schema()
            if tool.name == "apply_for_credit_card":
                params["properties"]["card_type"]["enum"] = list(
                    environment.user_tools.VALID_CREDIT_CARD_TYPES
                )
            forms.append(ProviderPortalForm(
                native_user_tool=tool.name, source_tool_name=tool.name,
                title=tool.short_desc or tool.name.replace("_", " ").title(),
                description=tool.long_desc or "", argument_schema=params,
            ))
        # Dynamic forms appear only after the native provider agent has granted
        # a specific discoverable customer tool in this conversation.
        if declared_user_tools is None or "call_discoverable_user_tool" in declared_user_tools:
            from tau2.environment.tool import as_tool

            granted = getattr(environment.tools, "_user_discoverable_tools_state", {})
            available = environment.user_tools.get_discoverable_tools()
            for name in sorted(set(granted) & set(available)):
                tool = as_tool(getattr(environment.user_tools, name))
                forms.append(ProviderPortalForm(
                    native_user_tool="call_discoverable_user_tool",
                    source_tool_name=name,
                    title=tool.short_desc or name.replace("_", " ").title(),
                    description=tool.long_desc or "",
                    argument_schema={
                        **tool.params.model_json_schema(),
                        "x-native-discoverable-name": name,
                    },
                ))
        return forms

    @staticmethod
    def _source_user_ids(provider_id: str, task, environment) -> list[str]:
        """Resolve source-linked customer IDs from native source data only."""
        if provider_id == "tau2-airline":
            known = getattr(task.user_scenario.instructions, "known_info", "") or ""
            match = re.search(r"\buser id is\s+([A-Za-z0-9_-]+)", known, re.IGNORECASE)
            user_id = match.group(1) if match else None
            return [user_id] if user_id and user_id in environment.tools.db.users else []
        if provider_id == "tau2-telecom":
            known = getattr(task.user_scenario.instructions, "known_info", "") or ""
            match = re.search(r"\bphone number\s+([+()0-9 .-]{7,})", known, re.IGNORECASE)
            if not match:
                return []
            digits = "".join(ch for ch in match.group(1) if ch.isdigit())
            db = environment.tools.db
            rows = [row.customer_id for row in db.customers
                    if "".join(ch for ch in row.phone_number if ch.isdigit()) == digits]
            return rows if len(rows) == 1 else []
        # Banking task initialization carries exact customer identifiers for
        # account-based cases. Tasks without a source-linked account (for
        # example a new card application) remain applications and are not
        # assigned an invented bank customer.
        initialization = task.initial_state.initialization_data if task.initial_state else None
        agent_data = initialization.agent_data if initialization else None
        if not isinstance(agent_data, dict):
            agent_data = {}
        users = (agent_data.get("users") or {}).get("data") or {}
        if users:
            return sorted(str(user_id) for user_id in users)
        data = []
        for table in agent_data.values():
            if isinstance(table, dict):
                data.extend((table.get("data") or {}).values())
        ids = {str(row["user_id"]) for row in data if isinstance(row, dict) and row.get("user_id")}
        if ids:
            return sorted(ids)
        instructions = task.user_scenario.instructions
        if not isinstance(instructions, str):
            return []
        name_patterns = (
            r"(?im)^\s*\*\*Your character:\*\*\s*(?:You are\s+)?([^,\n.]+)",
            r"(?im)^\s*Your character is\s+[^\n.]*?\bnamed\s+([^,\n.]+)",
            r"(?im)^\s*Your character is\s+([^,\n.]+)",
            r"(?im)^\s*(?:Your full name|Your name|Full name|Name)\s*:\s*([^,\n.]+)",
        )
        names = []
        for pattern in name_patterns:
            match = re.search(pattern, instructions)
            if match:
                candidate = re.split(r"\s+(?:a|an|who|from|and)\s+", match.group(1), maxsplit=1,
                                     flags=re.IGNORECASE)[0].strip(" *:")
                if len(candidate.split()) >= 2:
                    names.append(candidate)
                    break
        if not names:
            return []
        target = names[0].casefold()
        matches = [str(user_id) for user_id, user in environment.tools.db.users.data.items()
                   if str(user.get("name", "")).casefold() == target]
        return matches if len(matches) == 1 else []

    @staticmethod
    def _portal_account_view(provider_id: str, task, environment) -> ProviderBusinessAccount | None:
        user_ids = Tau2ExternalProvider._source_user_ids(provider_id, task, environment)
        if len(user_ids) != 1:
            return None
        user_id = user_ids[0]
        db = environment.tools.db
        if provider_id == "tau2-airline":
            user = db.users[user_id]
            fields = {"email": user.email, "membership": user.membership,
                      "reservation_count": len(user.reservations)}
            records = []
            for reservation_id in user.reservations:
                reservation = db.reservations.get(reservation_id)
                if reservation is not None:
                    raw = reservation.model_dump(mode="json")
                    records.append(ProviderBusinessRecord(
                        entity_type="Reservation", source_id=reservation_id,
                        display_name=f"Reservation {reservation_id}", fields=raw,
                    ))
            return ProviderBusinessAccount(
                entity_type="AirlineCustomer", source_id=user_id,
                display_name=f"{user.name.first_name} {user.name.last_name}",
                fields=fields, records=records,
            )
        if provider_id == "tau2-telecom":
            customer = next((row for row in db.customers if row.customer_id == user_id), None)
            if customer is None:
                return None
            records = []
            for line_id in customer.line_ids:
                line = next((row for row in db.lines if row.line_id == line_id), None)
                if line is not None:
                    records.append(ProviderBusinessRecord(
                        entity_type="MobileLine", source_id=line_id,
                        display_name=f"Mobile line {line.phone_number}",
                        fields=line.model_dump(mode="json"),
                    ))
            for bill_id in customer.bill_ids:
                bill = next((row for row in db.bills if row.bill_id == bill_id), None)
                if bill is not None:
                    records.append(ProviderBusinessRecord(
                        entity_type="Bill", source_id=bill_id,
                        display_name=f"Bill {bill_id}", fields=bill.model_dump(mode="json"),
                    ))
            return ProviderBusinessAccount(
                entity_type="TelecomCustomer", source_id=user_id,
                display_name=customer.full_name,
                fields={"phone_number": customer.phone_number, "email": customer.email},
                records=records,
            )
        users = db.users.data
        user = users.get(user_id)
        if user is None:
            return None
        records = []
        for table_name in ("accounts", "credit_card_accounts", "debit_cards", "credit_card_orders",
                           "transaction_disputes", "debit_card_disputes", "payment_history",
                           "bank_account_transaction_history", "credit_card_transaction_history"):
            table = getattr(db, table_name, None)
            for source_id, row in (getattr(table, "data", {}) or {}).items():
                if isinstance(row, dict) and row.get("user_id") == user_id:
                    records.append(ProviderBusinessRecord(
                        entity_type=table_name, source_id=str(source_id),
                        display_name=f"{table_name.replace('_', ' ').title()} {source_id}", fields=row,
                    ))
        return ProviderBusinessAccount(
            entity_type="BankingCustomer", source_id=user_id,
            display_name=str(user.get("name", user_id)),
            fields={key: user[key] for key in ("email", "phone_number", "address") if user.get(key)},
            records=records,
        )

    def _base_databases(self, provider_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        domain = self._domain(provider_id)
        directory = self.runtime_root / f"data/tau2/domains/{domain}"
        if domain == "airline":
            from tau2.domains.airline.data_model import FlightDB
            db = FlightDB.model_validate(json.loads((directory / "db.json").read_text()))
            return db.model_dump(mode="json"), None
        if domain == "banking_knowledge":
            from tau2.domains.banking_knowledge.data_model import TransactionalDB

            db = TransactionalDB.load(str(directory / "db.json"))
            return db.model_dump(mode="json"), None
        from tau2.domains.telecom.data_model import TelecomDB
        from tau2.domains.telecom.user_data_model import TelecomUserDB
        db = TelecomDB.model_validate(tomllib.loads((directory / "db.toml").read_text()))
        user_db = TelecomUserDB.model_validate(tomllib.loads((directory / "user_db.toml").read_text()))
        return db.model_dump(mode="json"), user_db.model_dump(mode="json")

    def _environment(self, provider_id: str, state: dict[str, Any], task=None):
        domain = self._domain(provider_id)
        directory = self.runtime_root / f"data/tau2/domains/{domain}"
        if domain == "airline":
            from tau2.domains.airline.data_model import FlightDB
            from tau2.domains.airline.environment import get_environment
            db = FlightDB.model_validate(state["agent_db"])
            return get_environment(db=db, policy=(directory / "policy.md").read_text(encoding="utf-8"))
        if domain == "banking_knowledge":
            from tau2.domains.banking_knowledge.data_model import KnowledgeBase, TransactionalDB
            from tau2.domains.banking_knowledge.environment import get_environment

            database = TransactionalDB.model_validate(state["agent_db"])
            knowledge_base = KnowledgeBase.load(str(directory / "documents"))
            read_log_allowlist = _banking_read_log_allowlist(task) if task else set()
            return get_environment(
                db=database, knowledge_base=knowledge_base, task=task,
                retrieval_variant="bm25_grep", read_log_allowlist=read_log_allowlist,
            )
        from tau2.domains.telecom.data_model import TelecomDB
        from tau2.domains.telecom.environment import get_environment
        from tau2.domains.telecom.user_data_model import TelecomUserDB
        db = TelecomDB.model_validate(state["agent_db"])
        user_db = TelecomUserDB.model_validate(state["user_db"])
        policy = "<main_policy>\n" + (directory / "main_policy.md").read_text(encoding="utf-8")
        policy += "\n</main_policy>\n<tech_support_policy>\n"
        policy += (directory / "tech_support_manual.md").read_text(encoding="utf-8")
        policy += "\n</tech_support_policy>"
        return get_environment(db=db, user_db=user_db, policy_type="manual", policy=policy)

    @staticmethod
    def _snapshot(environment, provider_id: str | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
        agent_db = environment.tools.db.model_dump(mode="json")
        user_db = (
            environment.user_tools.db.model_dump(mode="json")
            if environment.user_tools and provider_id != "tau2-banking" else None
        )
        return agent_db, user_db

    @staticmethod
    def _bank_state_evidence(before: dict[str, Any], after: dict[str, Any], request: ProviderRequest,
                             task_id: str, revision: str) -> ProviderBankStateEvidence | None:
        """Describe native Banking writes without exposing record values."""
        refs: list[NativeProviderReference] = []
        changed_fields = 0
        for table_name in sorted(set(before) | set(after)):
            before_table = before.get(table_name) or {}
            after_table = after.get(table_name) or {}
            before_rows = before_table.get("data", {}) if isinstance(before_table, dict) else {}
            after_rows = after_table.get("data", {}) if isinstance(after_table, dict) else {}
            for source_id in sorted(set(before_rows) | set(after_rows)):
                old, new = before_rows.get(source_id), after_rows.get(source_id)
                if old == new:
                    continue
                refs.append(NativeProviderReference(
                    domain="banking_knowledge", entity_type=table_name, source_id=str(source_id),
                ))
                changed_fields += len(set(old or {}) | set(new or {}))
        if not refs:
            return None
        facts = {
            "provider_id": "tau2-banking", "task_id": task_id,
            "conversation_id": request.conversation_id,
            "references": [ref.model_dump(mode="json") for ref in refs],
            "revision": revision,
        }
        return ProviderBankStateEvidence(
            evidence_id="tau2-provider-event:" + _canonical_hash(facts),
            event="banking_records_changed", company_id="company", provider_id="tau2-banking",
            task_id=task_id, conversation_id=request.conversation_id,
            company_actor_id=request.company_actor_id,
            company_account_reference=request.company_account_reference,
            company_issue_reference=request.company_issue_reference,
            native_state_revision=revision, native_references=refs,
            changed_field_count=max(1, changed_fields),
        )

    @staticmethod
    def _restore_banking_discoverable_tools(environment, trajectory: list[dict[str, Any]]) -> None:
        """Rebuild τ's in-memory discoverable-tool grants from the private transcript."""
        import json
        from tau2.domains.banking_knowledge.tools import parse_discoverable_tool_docstring

        messages = iter(trajectory)
        for message in messages:
            if message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls") or []
            for call in calls:
                # Only successful, persisted tool turns can establish a grant.
                result = next(messages, {})
                if result.get("role") != "tool" or result.get("error"):
                    continue
                arguments = call.get("arguments") or {}
                if call.get("name") == "unlock_discoverable_agent_tool":
                    name = arguments.get("agent_tool_name")
                    method = environment.tools.get_discoverable_tools().get(name)
                    if method is not None:
                        environment.tools._agent_discoverable_tools_state[name] = {
                            "unlocked_at": "restored-from-private-transcript",
                            "tool_info": parse_discoverable_tool_docstring(method),
                        }
                elif call.get("name") == "give_discoverable_user_tool":
                    name = arguments.get("discoverable_tool_name")
                    method = getattr(type(environment.user_tools), name, None)
                    if method is not None:
                        try:
                            supplied = json.loads(arguments.get("arguments", "{}"))
                        except (TypeError, json.JSONDecodeError):
                            continue
                        environment.tools._user_discoverable_tools_state[name] = {
                            "arguments": supplied,
                            "given_at": "restored-from-private-transcript",
                            "tool_info": parse_discoverable_tool_docstring(method),
                        }

    @staticmethod
    def _provider_currency(environment) -> str:
        settings = getattr(environment.tools, "regional_settings", None)
        return settings.currency_code if settings is not None else "USD"

    @staticmethod
    def _refuel_bill_evidence(environment, arguments: dict[str, Any], before_bills: dict[str, Any],
                              request: ProviderRequest, task_id: str) -> ProviderBillEventEvidence:
        """Build evidence only from the bill delta made by TelecomTools.refuel_data."""
        from decimal import Decimal

        customer_id = arguments.get("customer_id")
        line_id = arguments.get("line_id")
        customer = environment.tools.get_customer_by_id(customer_id)
        if line_id not in customer.line_ids:
            raise ExternalProviderError("native refuel result line is not owned by its source customer")
        changed = []
        for bill_id in customer.bill_ids:
            bill = environment.tools._get_bill_by_id(bill_id)
            prior = before_bills.get(bill_id)
            prior_items = prior.get("line_items", []) if prior else []
            new_items = bill.line_items[len(prior_items):]
            if len(new_items) == 1 and bill.status.value == "Draft":
                changed.append((bill, new_items[0], len(prior_items)))
        if len(changed) != 1:
            raise ExternalProviderError("native refuel write did not produce one identifiable draft bill charge")
        bill, item, item_index = changed[0]
        currency = Tau2ExternalProvider._provider_currency(environment)
        facts = {
            "provider_id": "tau2-telecom", "task_id": task_id,
            "conversation_id": request.conversation_id, "customer_id": customer_id,
            "line_id": line_id, "bill_id": bill.bill_id,
            "description": item.description, "amount": str(Decimal(str(item.amount))),
            "date": item.date.isoformat(), "charge_line_index": item_index,
        }
        return ProviderBillEventEvidence(
            evidence_id="tau2-provider-event:" + _canonical_hash(facts),
            event="refuel_charge_added", company_id=request.company_id,
            provider_id="tau2-telecom", task_id=task_id,
            conversation_id=request.conversation_id,
            company_actor_id=request.company_actor_id,
            company_account_reference=request.company_account_reference,
            company_issue_reference=request.company_issue_reference,
            native_state_revision="pending-final-state-revision",
            native_references=[
                {"domain": "telecom", "entity_type": "customer", "source_id": customer.customer_id},
                {"domain": "telecom", "entity_type": "line", "source_id": line_id},
                {"domain": "telecom", "entity_type": "bill", "source_id": bill.bill_id},
            ],
            provider_bill_status=bill.status.value, currency=currency,
            charge_line_index=item_index,
            charge_amount=Decimal(str(item.amount)), charge_description=item.description,
            provider_date=item.date.isoformat(), bill_total_due=Decimal(str(bill.total_due)),
            bill_period_start=bill.period_start.isoformat(),
            bill_period_end=bill.period_end.isoformat(), bill_due_date=bill.due_date.isoformat(),
        )

    def _bootstrap(self, provider_id: str) -> dict[str, Any]:
        agent_db, user_db = self._base_databases(provider_id)
        return self.store.bootstrap(provider_id, TAU2_SOURCE_COMMIT, agent_db, user_db)

    def _create_agent(self, environment):
        if self.agent_factory is not None:
            return self.agent_factory(environment, self.llm_alias)
        from tau2.agent.llm_agent import LLMAgent
        return LLMAgent(
            tools=environment.get_tools(), domain_policy=environment.get_policy(),
            llm=self.llm_alias, llm_args={"num_retries": 0},
        )

    def prepare_case(self, request: ProviderCasePrepareRequest | dict[str, Any]) -> ProviderCasePrepareResponse:
        """Apply source initialization and expose native customer records without model generation."""
        request = ProviderCasePrepareRequest.model_validate(request)
        task = self._load_task(request.provider_id, request.task_id)
        operation_id = "prepare:" + request.conversation_id
        operation = request.model_dump(mode="json")
        with self.store.locked_provider(request.provider_id):
            saved_case = self.store.conversation(request.provider_id, request.conversation_id)
            if saved_case is not None:
                if saved_case["task_id"] != request.task_id:
                    raise ExternalProviderError("a provider conversation cannot change source task identity")
                context = saved_case["company_context"]
                if any(context.get(key) != operation.get(key) for key in (
                    "company_id", "company_actor_id", "company_account_reference", "company_issue_reference",
                )):
                    raise ExternalProviderError("company case identity changed after native preparation")
                state = self.store.state(request.provider_id)
                if state is None:
                    raise RuntimeError("native provider state is missing after conversation preparation")
                environment = self._environment(request.provider_id, state, task)
                if request.provider_id == "tau2-banking":
                    config = context.get("banking_task_config")
                    if config is not None:
                        from tau2.data_model.tasks import InitializationData
                        environment.set_state(InitializationData.model_validate({
                            "agent_data": {"task_config": config},
                        }), None, [])
                    self._restore_banking_discoverable_tools(environment, saved_case["trajectory"])
                account = self._portal_account_view(request.provider_id, task, environment)
                forms = self._native_portal_forms(task, environment)
                status = saved_case["initialization_status"]
                business_context = self._case_business_context(request.provider_id, task, account)
                return ProviderCasePrepareResponse(
                    provider_id=request.provider_id, task_id=request.task_id,
                    conversation_id=request.conversation_id,
                    status="conflict" if status == "conflict" else "already_prepared",
                    initialization_status=status,
                    initialization_revision=saved_case["initialization_evidence"].get(
                        "provider_state_revision_after", state["state_revision"]
                    ),
                    company_account_reference=request.company_account_reference,
                    company_issue_reference=request.company_issue_reference,
                    business_description=business_context,
                    subject=business_context.splitlines()[0][:998],
                    requested_outcome=(self._banking_catalog_entry(request.task_id).requested_outcome
                                       if request.provider_id == "tau2-banking" else
                                       "Review the linked provider account and respond to the customer request."),
                    private_factual_context=self._banking_private_facts(request.provider_id, request.task_id),
                    account=account, forms=forms,
                )

            claim, claim_value = self.store.claim(request.provider_id, operation_id, operation)
            if claim == "conflict":
                raise ExternalProviderError("native preparation operation was reused with different case identity")
            if claim == "started_uncertain":
                saved_operation = self.store.operation(request.provider_id, operation_id)
                if saved_operation is None or saved_operation["request_hash"] != canonical_request_hash(operation):
                    raise RuntimeError("native preparation retry lost its original operation identity")
                # No provider/task mutation is durable until commit_operation writes
                # state, conversation, and receipt together; replay exact setup.
                claim_value = saved_operation["request_hash"]
            if claim == "completed":
                # A completed prepare operation must have committed its conversation
                # in the same transaction. Its absence signals corrupted state.
                raise RuntimeError("native preparation receipt exists without its prepared conversation")

            state = self._bootstrap(request.provider_id)
            before_init = {"agent_db": deepcopy(state["agent_db"]), "user_db": deepcopy(state["user_db"])}
            baseline = None
            conversation_baseline = None
            if request.provider_id == "tau2-banking":
                conversation_baseline = before_init
            elif self.store.task_baseline(request.provider_id, request.task_id) is None:
                baseline = before_init

            environment = self._environment(request.provider_id, state, task)
            initialized = set(state["initialized_task_ids"])
            status = "already_applied" if request.task_id in initialized else "applied"
            conflict_count = 0
            error_type = None
            banking_task_config = None
            if request.task_id not in initialized:
                initial = task.initial_state
                initial_data = initial.initialization_data if initial else None
                initial_actions = initial.initialization_actions if initial else None
                initial_history = initial.message_history if initial else []
                if request.provider_id == "tau2-banking":
                    raw_data = initial_data.model_dump(mode="json", exclude_none=True) if initial_data else None
                    banking_task_config = _banking_task_config_patch(initial_data)
                    source_db, _ = self._base_databases(request.provider_id)
                    conflicts = _banking_patch_conflicts(state["agent_db"], source_db, raw_data)
                    conflict_count = len(conflicts)
                    candidate = self._environment(request.provider_id, state, task)
                    if conflicts:
                        status = "conflict"
                        error_type = "shared_provider_record_conflict"
                    else:
                        try:
                            if initial_data is not None and banking_task_config is not None:
                                from tau2.data_model.tasks import InitializationData
                                agent_data = dict(initial_data.agent_data or {})
                                agent_data.pop("task_config", None)
                                business_data = initial_data.model_copy(update={"agent_data": agent_data})
                                candidate.set_state(business_data, initial_actions, initial_history or [])
                                patched, _ = self._snapshot(candidate, request.provider_id)
                                task_env = self._environment(request.provider_id,
                                    {**state, "agent_db": patched, "user_db": None}, task)
                                task_env.set_state(InitializationData.model_validate({
                                    "agent_data": {"task_config": banking_task_config},
                                }), None, [])
                                environment = task_env
                            else:
                                candidate.set_state(initial_data, initial_actions, initial_history or [])
                        except (ValueError, KeyError):
                            status = "conflict"
                            error_type = "native_source_initialization_conflict"
                        else:
                            if banking_task_config is None:
                                environment = candidate
                            state["agent_db"], state["user_db"] = self._snapshot(candidate, request.provider_id)
                elif initial_data is not None:
                    status = "conflict"
                    error_type = "source_initialization_data_requires_review"
                elif initial_actions or initial_history:
                    candidate = self._environment(request.provider_id, state, task)
                    try:
                        candidate.set_state(None, initial_actions, initial_history or [])
                    except ValueError:
                        status = "conflict"
                        error_type = "native_source_setup_conflict"
                    else:
                        environment = candidate
                        state["agent_db"], state["user_db"] = self._snapshot(environment, request.provider_id)
                if status != "conflict":
                    initialized.add(request.task_id)
            elif request.provider_id == "tau2-banking":
                banking_task_config = _banking_task_config_patch(
                    task.initial_state.initialization_data if task.initial_state else None
                )
                if banking_task_config is not None:
                    from tau2.data_model.tasks import InitializationData
                    environment.set_state(InitializationData.model_validate({
                        "agent_data": {"task_config": banking_task_config},
                    }), None, [])

            source_history = task.initial_state.message_history if task.initial_state else []
            agent = self._create_agent(environment)
            agent_state = agent.get_init_state(source_history or [])
            trajectory = [_private_json(message) for message in (source_history or [])]
            agent_db, user_db = self._snapshot(environment, request.provider_id)
            if request.provider_id == "tau2-banking" and banking_task_config is not None:
                agent_db["task_config"] = deepcopy(state["agent_db"].get("task_config", {"data": {}, "notes": ""}))
            state["agent_db"], state["user_db"] = agent_db, user_db
            revision = _canonical_hash({"agent_db": agent_db, "user_db": user_db})
            if request.provider_id == "tau2-banking":
                initialization_revision = revision
            else:
                initialization_revision = revision
            initialization_evidence = {
                "task_id": request.task_id, "status": status,
                "provider_state_revision_before": self.store._revision(before_init["agent_db"], before_init["user_db"]),
                "provider_state_revision_after": revision,
                "source_initialization_action_count": len(initial_actions or []) if task.initial_state else 0,
                "source_initialization_data_present": bool(task.initial_state and task.initial_state.initialization_data),
                "conflicting_table_count": conflict_count, "error_type": error_type,
            }
            state["initialized_task_ids"] = sorted(initialized)
            source_user_ids = self._source_user_ids(request.provider_id, task, environment)
            account = self._portal_account_view(request.provider_id, task, environment)
            business_context = self._case_business_context(request.provider_id, task, account)
            forms = self._native_portal_forms(task, environment)
            context = {
                "company_id": request.company_id,
                "company_actor_id": request.company_actor_id,
                "company_account_reference": request.company_account_reference,
                "company_issue_reference": request.company_issue_reference,
                "message_ids": [], "in_reply_to": None,
                "operation_ids": [operation_id],
                "native_state_revision": revision,
                "banking_task_config": banking_task_config if request.provider_id == "tau2-banking" else None,
                "provider_customer_source_id": source_user_ids[0] if len(source_user_ids) == 1 else None,
                "mapped_provider_user_id": source_user_ids[0] if request.provider_id in {"tau2-banking", "tau2-telecom"} and len(source_user_ids) == 1 else None,
            }
            prepared_case = {
                "agent_state": _private_json(agent_state), "trajectory": trajectory,
                "initialization_status": status,
                "initialization_evidence": initialization_evidence,
                "company_context": context,
            }
            response = ProviderCasePrepareResponse(
                provider_id=request.provider_id, task_id=request.task_id,
                conversation_id=request.conversation_id,
                status="conflict" if status == "conflict" else "prepared",
                initialization_status=status,
                initialization_revision=initialization_revision,
                company_account_reference=request.company_account_reference,
                company_issue_reference=request.company_issue_reference,
                business_description=business_context,
                subject=business_context.splitlines()[0][:998],
                requested_outcome=(self._banking_catalog_entry(request.task_id).requested_outcome
                                   if request.provider_id == "tau2-banking" else
                                   "Review the linked provider account and respond to the customer request."),
                private_factual_context=self._banking_private_facts(request.provider_id, request.task_id),
                account=account, forms=forms,
            )
            self.store.commit_operation(
                provider_id=request.provider_id, operation_id=operation_id,
                request_hash=claim_value, state=state,
                conversation_id=request.conversation_id, task_id=request.task_id,
                conversation=prepared_case, baseline=baseline,
                conversation_baseline=conversation_baseline,
                before_state=before_init,
                response=response.model_dump(mode="json"),
            )
            return response

    def handle(self, request: ProviderRequest | dict[str, Any]) -> ProviderResponse:
        request = ProviderRequest.model_validate(request)
        task = self._load_task(request.provider_id, request.task_id)
        operation = request.model_dump(mode="json")
        with self.store.locked_provider(request.provider_id):
            saved_case = self.store.conversation(request.provider_id, request.conversation_id)
            if saved_case and saved_case["task_id"] != request.task_id:
                raise ExternalProviderError("a provider conversation cannot change source task identity")
            expected_context = saved_case["company_context"] if saved_case else None
            if expected_context and any(expected_context.get(key) != request.model_dump(mode="json").get(key)
                                        for key in ("company_id", "company_actor_id", "company_account_reference", "company_issue_reference")):
                raise ExternalProviderError("company actor, account, or issue identity changed within the provider conversation")
            claim, claim_value = self.store.claim(request.provider_id, request.operation_id, operation)
            if claim == "conflict":
                raise ExternalProviderError("operation_id was reused with a different company request")
            if claim == "started_uncertain":
                return ProviderResponse(
                    provider_id=request.provider_id, operation_id=request.operation_id,
                    company_id=request.company_id, company_actor_id=request.company_actor_id,
                    company_account_reference=request.company_account_reference,
                    company_issue_reference=request.company_issue_reference,
                    received_message_id=request.message_id,
                    conversation_id=request.conversation_id, task_id=request.task_id,
                    status="started_uncertain", body=None, native_source=TAU2_SOURCE_COMMIT,
                    initialization_status="conflict", in_reply_to=request.message_id,
                    error_code="provider_operation_uncertain",
                )
            if claim == "completed":
                return ProviderResponse.model_validate(claim_value)

            state = self._bootstrap(request.provider_id)
            operation_before_state = {
                "agent_db": deepcopy(state["agent_db"]), "user_db": deepcopy(state["user_db"]),
            }
            banking_state_before_request = (
                deepcopy(state["agent_db"]) if request.provider_id == "tau2-banking" else None
            )
            baseline = None
            conversation_baseline = None
            if request.provider_id == "tau2-banking":
                conversation_baseline = self.store.conversation_baseline(
                    request.provider_id, request.conversation_id,
                )
                if conversation_baseline is None:
                    conversation_baseline = deepcopy({"agent_db": state["agent_db"], "user_db": None})
            elif self.store.task_baseline(request.provider_id, request.task_id) is None:
                baseline = {"agent_db": state["agent_db"], "user_db": state["user_db"]}

            environment = self._environment(request.provider_id, state, task)
            initialized = set(state["initialized_task_ids"])
            initialization_status = "already_applied"
            before_init_revision = _canonical_hash({"agent_db": state["agent_db"], "user_db": state["user_db"]})
            initialization_error_type = None
            banking_task_config = None
            if request.provider_id == "tau2-banking":
                if saved_case:
                    banking_task_config = saved_case["company_context"].get("banking_task_config")
                    if banking_task_config is not None:
                        from tau2.data_model.tasks import InitializationData
                        candidate = self._environment(request.provider_id, state, task)
                        candidate.set_state(
                            InitializationData.model_validate({
                                "agent_data": {"task_config": banking_task_config},
                            }), None, [],
                        )
                        environment = candidate
                    initialization_status = saved_case["initialization_status"]
                else:
                    initial = task.initial_state
                    initial_data = initial.initialization_data if initial else None
                    initial_actions = initial.initialization_actions if initial else None
                    initial_history = initial.message_history if initial else []
                    raw_initial_data = initial_data.model_dump(mode="json", exclude_none=True) if initial_data else None
                    banking_task_config = _banking_task_config_patch(initial_data)
                    canonical_db, _ = self._base_databases(request.provider_id)
                    conflicting_tables = _banking_patch_conflicts(
                        state["agent_db"], canonical_db, raw_initial_data,
                    )
                    candidate = self._environment(request.provider_id, state, task)
                    if not conflicting_tables:
                        try:
                            if initial_data is not None and banking_task_config is not None:
                                # task_config is per conversation; apply source business data
                                # to the shared provider state and the config only to this env.
                                from tau2.data_model.tasks import InitializationData
                                agent_data = dict(initial_data.agent_data or {})
                                agent_data.pop("task_config", None)
                                business_data = initial_data.model_copy(update={"agent_data": agent_data})
                                candidate.set_state(business_data, initial_actions, initial_history or [])
                                patched_state, _ = self._snapshot(candidate, request.provider_id)
                                task_state = {**state, "agent_db": patched_state, "user_db": None}
                                task_env = self._environment(request.provider_id, task_state, task)
                                task_env.set_state(InitializationData.model_validate({
                                    "agent_data": {"task_config": banking_task_config},
                                }), None, [])
                                environment = task_env
                            else:
                                candidate.set_state(initial_data, initial_actions, initial_history or [])
                        except (ValueError, KeyError):
                            initialization_status = "conflict"
                            initialization_error_type = "native_source_initialization_conflict"
                        else:
                            if banking_task_config is None:
                                environment = candidate
                            initialization_status = "applied"
                            state["agent_db"], state["user_db"] = self._snapshot(candidate, request.provider_id)
                    else:
                        initialization_status = "conflict"
                        initialization_error_type = "shared_provider_record_conflict"
                    if initialization_status == "conflict" and banking_task_config is not None:
                        from tau2.data_model.tasks import InitializationData
                        environment = self._environment(request.provider_id, state, task)
                        environment.set_state(
                            InitializationData.model_validate({
                                "agent_data": {"task_config": banking_task_config},
                            }), None, [],
                        )
                    initialized.add(request.task_id)
            elif request.task_id not in initialized:
                initial = task.initial_state
                initial_data = initial.initialization_data if initial else None
                initial_actions = initial.initialization_actions if initial else None
                initial_history = initial.message_history if initial else []
                if initial_data is not None:
                    # The supported source sets currently use native initialization
                    # actions. Data replacement patches need a conflict-aware mapping.
                    initialization_status = "conflict"
                    initialization_error_type = "source_initialization_data_requires_review"
                elif initial_actions or initial_history:
                    try:
                        candidate = self._environment(request.provider_id, state, task)
                        candidate.set_state(None, initial_actions, initial_history or [])
                    except ValueError:
                        # Preserve current shared state and keep handling the actual
                        # company message against that state; report the conflict.
                        initialization_status = "conflict"
                        initialization_error_type = "native_source_setup_conflict"
                    else:
                        environment = candidate
                        state["agent_db"], state["user_db"] = self._snapshot(environment, request.provider_id)
                        initialized.add(request.task_id)
                        initialization_status = "applied"
                else:
                    initialized.add(request.task_id)
                    initialization_status = "applied"
            elif saved_case:
                initialization_status = saved_case["initialization_status"]
            after_init_agent_db, after_init_user_db = self._snapshot(environment, request.provider_id)
            if request.provider_id == "tau2-banking" and banking_task_config is not None:
                # Persist no per-conversation task_config rows in the shared bank DB.
                after_init_agent_db["task_config"] = deepcopy(state["agent_db"].get("task_config", {"data": {}, "notes": ""}))
            initialization_evidence = saved_case["initialization_evidence"] if saved_case else {
                "task_id": request.task_id,
                "status": initialization_status,
                "provider_state_revision_before": before_init_revision,
                "provider_state_revision_after": _canonical_hash({
                    "agent_db": after_init_agent_db, "user_db": after_init_user_db,
                }),
                "source_initialization_action_count": len(
                    task.initial_state.initialization_actions
                    if task.initial_state and task.initial_state.initialization_actions else []
                ),
                "source_initialization_data_present": bool(
                    task.initial_state and task.initial_state.initialization_data is not None
                ),
                "conflicting_table_count": len(conflicting_tables) if request.provider_id == "tau2-banking" and not saved_case else 0,
                "error_type": initialization_error_type,
            }
            state["initialized_task_ids"] = sorted(initialized)

            from tau2.agent.llm_agent import LLMAgentState
            from tau2.data_model.message import MultiToolMessage, UserMessage
            agent = self._create_agent(environment)
            if saved_case is None:
                history = task.initial_state.message_history if task.initial_state else []
                agent_state = agent.get_init_state(history or [])
                trajectory = [_private_json(message) for message in (history or [])]
            else:
                agent_state = LLMAgentState.model_validate(saved_case["agent_state"])
                trajectory = saved_case["trajectory"]
            if request.provider_id == "tau2-banking":
                self._restore_banking_discoverable_tools(environment, trajectory)
            inbound = f"Subject: {request.subject}\n\n{request.body}" if request.subject else request.body
            user_message = UserMessage(role="user", content=inbound)
            trajectory.append(_private_json(user_message))
            assistant_message, agent_state = agent.generate_next_message(user_message, agent_state)
            trajectory.append(_private_json(assistant_message))
            tool_turns = 0
            native_bill_evidence: list[ProviderBillEventEvidence] = []
            while assistant_message.tool_calls:
                tool_turns += 1
                if tool_turns > 100:
                    raise ExternalProviderError("native τ agent exceeded the upstream 100-step bound")
                tool_messages = []
                for call in assistant_message.tool_calls:
                    if call.requestor == "assistant":
                        before_bills = {
                            bill.bill_id: bill.model_dump(mode="json")
                            for bill in getattr(environment.tools.db, "bills", [])
                        }
                        tool_result = environment.get_response(call)
                        tool_messages.append(tool_result)
                        if call.name == "refuel_data" and not tool_result.error:
                            native_bill_evidence.append(self._refuel_bill_evidence(
                                environment, call.arguments, before_bills, request, task.id,
                            ))
                    else:
                        from tau2.data_model.message import ToolMessage
                        tool_messages.append(ToolMessage(
                            id=call.id, role="tool", requestor="assistant",
                            content="Call rejected: external-provider agents cannot execute company-side user tools.",
                            error=True, execution_status="rejected",
                        ))
                trajectory.extend(_private_json(message) for message in tool_messages)
                environment.sync_tools()
                assistant_message, agent_state = agent.generate_next_message(
                    MultiToolMessage(role="tool", tool_messages=tool_messages), agent_state
                )
                trajectory.append(_private_json(assistant_message))
            if not assistant_message.content or not assistant_message.content.strip():
                raise ExternalProviderError("native τ provider agent returned no customer-facing reply")

            agent_db, user_db = self._snapshot(environment, request.provider_id)
            bank_before_db = banking_state_before_request
            if request.provider_id == "tau2-banking":
                # Bank task_config is scoped to the conversation and cannot leak into
                # another company's case in the shared provider database.
                agent_db["task_config"] = deepcopy(state["agent_db"].get("task_config", {"data": {}, "notes": ""}))
            state["agent_db"], state["user_db"] = agent_db, user_db
            revision = _canonical_hash({"agent_db": agent_db, "user_db": user_db})
            native_bill_evidence = [evidence.model_copy(update={"native_state_revision": revision})
                                    for evidence in native_bill_evidence]
            case = {
                "agent_state": _private_json(agent_state), "trajectory": trajectory,
                "initialization_status": initialization_status,
                "initialization_evidence": initialization_evidence,
                "company_context": {
                    "company_id": request.company_id,
                    "company_actor_id": request.company_actor_id,
                    "company_account_reference": request.company_account_reference,
                    "company_issue_reference": request.company_issue_reference,
                    "message_ids": (saved_case or {}).get("company_context", {}).get("message_ids", [])
                                   + [request.message_id],
                    "operation_ids": (saved_case or {}).get("company_context", {}).get("operation_ids", [])
                                     + [request.operation_id],
                    "in_reply_to": request.in_reply_to,
                    "native_state_revision": revision,
                    "banking_task_config": banking_task_config if request.provider_id == "tau2-banking" else None,
                },
            }
            bank_evidence = self._bank_state_evidence(
                bank_before_db, agent_db, request, task.id, revision,
            ) if bank_before_db is not None else None
            response = ProviderResponse(
                provider_id=request.provider_id, operation_id=request.operation_id,
                company_id=request.company_id, company_actor_id=request.company_actor_id,
                company_account_reference=request.company_account_reference,
                company_issue_reference=request.company_issue_reference,
                received_message_id=request.message_id,
                conversation_id=request.conversation_id, task_id=request.task_id,
                status="completed", body=assistant_message.content,
                native_source=TAU2_SOURCE_COMMIT, native_state_revision=revision,
                evidence=[*native_bill_evidence, *([bank_evidence] if bank_evidence else [])],
                initialization_status=initialization_status, in_reply_to=request.message_id,
                error_code="source_initialization_conflict" if initialization_status == "conflict" else None,
            )
            self.store.commit_operation(
                provider_id=request.provider_id, operation_id=request.operation_id,
                request_hash=claim_value, state=state, conversation_id=request.conversation_id,
                task_id=request.task_id, conversation=case, baseline=baseline,
                conversation_baseline=conversation_baseline,
                before_state=operation_before_state,
                response=response.model_dump(mode="json"),
            )
            return response

    def apply_company_payment(
        self, *, operation_id: str, conversation_id: str,
        evidence: CompanyPaymentEvidence | dict[str, Any],
    ) -> ProviderPaymentReceipt:
        """Record only a posted ERP payment as τ's native customer payment action."""
        evidence = CompanyPaymentEvidence.model_validate(evidence)
        provider_id = "tau2-telecom"
        case = self.store.conversation(provider_id, conversation_id)
        if case is None:
            raise ProviderTaskNotFound("native telecom conversation was not found")
        company_context = case["company_context"]
        if any(company_context.get(key) != getattr(evidence, key) for key in (
            "company_id", "company_actor_id", "company_account_reference", "company_issue_reference",
        )):
            raise ExternalProviderError("submitted payment does not match the linked company account and issue")
        task = self._load_task(provider_id, case["task_id"])
        actions = task.evaluation_criteria.actions if task.evaluation_criteria else []
        if not any(action.requestor == "user" and action.name == "make_payment" for action in (actions or [])):
            raise ExternalProviderError("the selected source task has no native customer payment action")
        request_value = {
            "provider_id": provider_id, "operation_id": operation_id,
            "conversation_id": conversation_id, "evidence": evidence.model_dump(mode="json"),
        }
        with self.store.locked_provider(provider_id):
            claim, claim_value = self.store.claim(provider_id, operation_id, request_value)
            if claim == "conflict":
                raise ExternalProviderError("operation_id was reused with different submitted payment evidence")
            if claim == "started_uncertain":
                raise ExternalProviderError("provider payment propagation is uncertain; reconcile the same operation")
            if claim == "completed":
                return ProviderPaymentReceipt.model_validate(claim_value)
            state = self.store.state(provider_id)
            if state is None:
                raise RuntimeError("native telecom provider state is not initialized")
            operation_before_state = {
                "agent_db": deepcopy(state["agent_db"]), "user_db": deepcopy(state["user_db"]),
            }
            environment = self._environment(provider_id, state)
            from decimal import Decimal
            from tau2.agent.llm_agent import LLMAgentState
            from tau2.data_model.message import ToolCall, UserMessage

            payment_request = environment.user_tools.db.surroundings.payment_request
            if payment_request is None:
                raise ExternalProviderError("native telecom has no outstanding payment request")
            if payment_request.bill_id != evidence.provider_bill_id:
                raise ExternalProviderError("submitted company payment references a different provider bill")
            if (Decimal(str(payment_request.amount_due)) != evidence.amount
                    or evidence.currency != self._provider_currency(environment)):
                raise ExternalProviderError("submitted company payment does not match the native provider bill")
            agent_state = LLMAgentState.model_validate(case["agent_state"])
            call = ToolCall(id=f"company-payment:{evidence.erp_name}", name="make_payment",
                            arguments={}, requestor="user")
            user_action = UserMessage(role="user", tool_calls=[call])
            tool_result = environment.get_response(call)
            if tool_result.error:
                raise ExternalProviderError("native τ user payment tool rejected the submitted ERP evidence")
            environment.sync_tools()
            provider_bill = environment.tools._get_bill_by_id(payment_request.bill_id)
            if provider_bill.status.value != "Paid":
                raise ExternalProviderError("native τ user payment did not reconcile to a paid provider bill")
            agent_state.messages.extend([user_action, tool_result])
            trajectory = [*case["trajectory"], _private_json(user_action), _private_json(tool_result)]
            agent_db, user_db = self._snapshot(environment)
            state["agent_db"], state["user_db"] = agent_db, user_db
            revision = _canonical_hash({"agent_db": agent_db, "user_db": user_db})
            receipt = ProviderPaymentReceipt(
                provider_id=provider_id, conversation_id=conversation_id,
                source_task_id=case["task_id"], erp_doctype=evidence.erp_doctype,
                erp_name=evidence.erp_name, provider_bill_id=evidence.provider_bill_id,
                provider_bill_status=provider_bill.status.value,
                amount=evidence.amount, currency=evidence.currency,
                native_result=str(tool_result.content), native_state_revision=revision,
                native_references=[*evidence.native_references, {
                    "domain": "telecom", "entity_type": "bill", "source_id": provider_bill.bill_id,
                }],
            )
            next_case = {
                "agent_state": _private_json(agent_state), "trajectory": trajectory,
                "initialization_status": case["initialization_status"],
                "initialization_evidence": case["initialization_evidence"],
                "company_context": {
                    **case["company_context"],
                    "operation_ids": case["company_context"].get("operation_ids", []) + [operation_id],
                },
            }
            self.store.complete_external_action(
                provider_id=provider_id, operation_id=operation_id,
                request_hash=claim_value, state=state, conversation_id=conversation_id,
                task_id=case["task_id"], conversation=next_case,
                response=receipt.model_dump(mode="json"),
                before_state=operation_before_state,
            )
            return receipt

    @staticmethod
    def _bank_account_owner(db, key: str) -> str | None:
        for table_name in ("accounts", "credit_card_accounts"):
            record = db.model_dump(mode="python").get(table_name, {}).get("data", {}).get(key)
            if record:
                return record.get("user_id")
        return None

    @staticmethod
    def _bank_payment_effect_matches(tool_name: str, arguments: dict[str, Any],
                                     before: dict[str, Any], after: dict[str, Any],
                                     amount: Decimal) -> bool:
        """Check the upstream Banking tool's actual account balance effects."""
        from tau2.domains.banking_knowledge.tools import _get_account_balance, _parse_balance

        before_accounts = before.get("accounts", {}).get("data", {})
        after_accounts = after.get("accounts", {}).get("data", {})
        if tool_name == "pay_credit_card_from_checking_9182":
            checking_id = arguments["checking_account_id"]
            card_id = arguments["credit_card_account_id"]
            before_cards = before.get("credit_card_accounts", {}).get("data", {})
            after_cards = after.get("credit_card_accounts", {}).get("data", {})
            return (
                checking_id in before_accounts and checking_id in after_accounts
                and card_id in before_cards and card_id in after_cards
                and Decimal(str(_get_account_balance(before_accounts[checking_id])))
                    - Decimal(str(_get_account_balance(after_accounts[checking_id]))) == amount
                and Decimal(str(_parse_balance(before_cards[card_id].get("current_balance", 0))))
                    - Decimal(str(_parse_balance(after_cards[card_id].get("current_balance", 0)))) == amount
            )
        if tool_name == "transfer_funds_between_bank_accounts_7291":
            source_id = arguments["source_account_id"]
            destination_id = arguments["destination_account_id"]
            return (
                source_id in before_accounts and source_id in after_accounts
                and destination_id in before_accounts and destination_id in after_accounts
                and Decimal(str(_get_account_balance(before_accounts[source_id])))
                    - Decimal(str(_get_account_balance(after_accounts[source_id]))) == amount
                and Decimal(str(_get_account_balance(after_accounts[destination_id])))
                    - Decimal(str(_get_account_balance(before_accounts[destination_id]))) == amount
            )
        return False

    @staticmethod
    def _decode_native_tool_result(content: str) -> str:
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            return content
        return decoded if isinstance(decoded, str) else json.dumps(decoded, sort_keys=True)

    def _bank_action_context(self, provider_id: str, conversation_id: str,
                             actor_id: str, account_ref: str, issue_ref: str):
        if provider_id not in {"tau2-banking", "tau2-telecom"}:
            raise ExternalProviderError("the selected provider has no native customer portal actions")
        case = self.store.conversation(provider_id, conversation_id)
        if case is None:
            raise ProviderTaskNotFound("native provider conversation was not found")
        context = case["company_context"]
        if any(context.get(key) != value for key, value in (
            ("company_actor_id", actor_id), ("company_account_reference", account_ref),
            ("company_issue_reference", issue_ref),
        )):
            raise ExternalProviderError("company actor, account, or issue does not match the provider conversation")
        state = self.store.state(provider_id)
        if state is None:
            raise RuntimeError("native provider state is not initialized")
        task = self._load_task(provider_id, case["task_id"])
        environment = self._environment(provider_id, state, task)
        task_config = case["company_context"].get("banking_task_config")
        if task_config is not None:
            from tau2.data_model.tasks import InitializationData
            environment.set_state(InitializationData.model_validate({
                "agent_data": {"task_config": task_config},
            }), None, [])
        self._restore_banking_discoverable_tools(environment, case["trajectory"])
        return case, state, task, environment

    def apply_company_portal_action(
        self, action: CompanyProviderPortalAction | dict[str, Any],
    ) -> ProviderBankActionReceipt:
        """Apply a verified company-side user action through τ's native user toolkit."""
        action = CompanyProviderPortalAction.model_validate(action)
        action_value = action.model_dump(mode="json")
        with self.store.locked_provider(action.provider_id):
            case, state, task, environment = self._bank_action_context(
                action.provider_id, action.conversation_id, action.company_actor_id,
                action.company_account_reference, action.company_issue_reference,
            )
            operation_before_state = {
                "agent_db": deepcopy(state["agent_db"]), "user_db": deepcopy(state["user_db"]),
            }
            prior_operation = self.store.operation(action.provider_id, action.operation_id)
            if prior_operation is not None:
                if prior_operation["request_hash"] != canonical_request_hash(action_value):
                    raise ExternalProviderError("operation_id was reused with different verified portal evidence")
                if prior_operation["status"] == "completed":
                    return ProviderBankActionReceipt.model_validate(prior_operation["response"])
                raise ExternalProviderError("native Banking portal action is uncertain; reconcile the same operation")
            user_id = action.mapped_provider_user_id
            if action.provider_id == "tau2-banking" and user_id is not None and user_id not in environment.tools.db.users.data:
                raise ExternalProviderError("mapped provider user does not exist in the native Banking database")
            if action.provider_id == "tau2-telecom" and user_id is not None and not any(
                row.customer_id == user_id for row in environment.tools.db.customers
            ):
                raise ExternalProviderError("mapped provider customer does not exist in the native Telecom database")
            existing_user = case["company_context"].get("mapped_provider_user_id")
            if user_id is not None and existing_user is not None and existing_user != user_id:
                raise ExternalProviderError("provider conversation is already bound to another native customer")
            arguments = dict(action.arguments)
            effective_arguments = arguments
            named_user = arguments.get("user_id")
            if named_user is not None and (user_id is None or named_user != user_id):
                raise ExternalProviderError("portal action customer does not match the mapped native customer")
            # Delegate None-versus-empty semantics to the pinned τ environment:
            # None means all customer tools; [] means no customer tools.
            native_user_tools = environment.get_user_tools(include=task.user_tools)
            native_tools_by_name = {tool.name: tool for tool in native_user_tools}
            if action.native_user_tool not in native_tools_by_name and action.native_user_tool != "call_discoverable_user_tool":
                raise ExternalProviderError("requested operation is not an available native τ customer tool")
            if action.native_user_tool == "call_discoverable_user_tool":
                if task.user_tools is not None and "call_discoverable_user_tool" not in task.user_tools:
                    raise ExternalProviderError("discoverable customer tools are not enabled for this task")
                discoverable_name = arguments.get("discoverable_tool_name")
                if discoverable_name not in environment.tools._user_discoverable_tools_state:
                    raise ExternalProviderError("the native user tool was not given during this provider conversation")
                try:
                    inner = json.loads(arguments.get("arguments", "{}"))
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ExternalProviderError("native discoverable user tool arguments are invalid") from exc
                if inner.get("user_id") is not None and inner["user_id"] != user_id:
                    raise ExternalProviderError("discoverable action user does not match the mapped native user")
                effective_arguments = inner
                arguments["arguments"] = json.dumps(inner, sort_keys=True)
            if action.provider_id == "tau2-telecom":
                customers = {row.customer_id: row for row in environment.tools.db.customers}
                if user_id is None or user_id not in customers:
                    raise ExternalProviderError("telecom customer action requires the case's exact source customer")
                customer = customers[user_id]
                customer_id = effective_arguments.get("customer_id")
                if customer_id is not None and customer_id != user_id:
                    raise ExternalProviderError("telecom action customer does not match the linked customer")
                line_id = effective_arguments.get("line_id")
                if line_id is not None and line_id not in customer.line_ids:
                    raise ExternalProviderError("telecom line does not belong to the linked customer")
                bill_id = effective_arguments.get("bill_id")
                if bill_id is not None and bill_id not in customer.bill_ids:
                    raise ExternalProviderError("telecom bill does not belong to the linked customer")
            elif user_id is not None and user_id not in environment.tools.db.users.data:
                raise ExternalProviderError("mapped provider user does not exist in the native Banking database")
            for field in ("account_id", "checking_account_id", "credit_card_account_id",
                          "source_account_id", "destination_account_id"):
                account_id = effective_arguments.get(field)
                if account_id is not None and (
                    user_id is None or self._bank_account_owner(environment.tools.db, account_id) != user_id
                ):
                    raise ExternalProviderError("native account does not belong to the mapped provider user")

            claim, claim_value = self.store.claim(action.provider_id, action.operation_id, action_value)
            if claim != "claimed":
                raise ExternalProviderError("native Banking portal action could not acquire its operation claim")
            from tau2.data_model.message import ToolCall, UserMessage
            call = ToolCall(
                id=f"company-portal:{action.operation_id}", name=action.native_user_tool,
                arguments=arguments, requestor="user",
            )
            result = environment.get_response(call)
            decoded = self._decode_native_tool_result(result.content)
            environment.sync_tools()
            if result.error or decoded.startswith("Error:") or decoded.startswith("Failed:"):
                receipt = ProviderBankActionReceipt(
                    provider_id=action.provider_id, operation_id=action.operation_id,
                    conversation_id=action.conversation_id, status="rejected",
                    native_tool=action.native_user_tool, native_result_hash=_canonical_hash(decoded),
                    native_state_revision=state["state_revision"],
                    source_references=action.evidence_references,
                )
                self.store.complete_external_action(
                    provider_id=action.provider_id, operation_id=action.operation_id,
                    request_hash=claim_value, state=state, conversation_id=action.conversation_id,
                    task_id=case["task_id"], conversation=case,
                    response=receipt.model_dump(mode="json"),
                    before_state=operation_before_state,
                )
                return receipt
            trajectory = [*case["trajectory"], _private_json(UserMessage(role="user", tool_calls=[call])),
                          _private_json(result)]
            from tau2.agent.llm_agent import LLMAgentState
            agent_state = LLMAgentState.model_validate(case["agent_state"])
            agent_state.messages.extend([UserMessage(role="user", tool_calls=[call]), result])
            agent_db, user_db = self._snapshot(environment, action.provider_id)
            revision = _canonical_hash({"agent_db": agent_db, "user_db": user_db})
            state["agent_db"], state["user_db"] = agent_db, user_db
            next_case = {
                "agent_state": _private_json(agent_state), "trajectory": trajectory,
                "initialization_status": case["initialization_status"],
                "initialization_evidence": case["initialization_evidence"],
                "company_context": {**case["company_context"],
                                    "operation_ids": case["company_context"].get("operation_ids", []) + [action.operation_id],
                                    **({"mapped_provider_user_id": user_id} if user_id is not None else {}),
                                    "native_state_revision": revision},
            }
            receipt = ProviderBankActionReceipt(
                provider_id=action.provider_id, operation_id=action.operation_id,
                conversation_id=action.conversation_id, status="applied", native_tool=action.native_user_tool,
                native_result_hash=_canonical_hash(decoded), native_state_revision=revision,
                source_references=action.evidence_references,
            )
            self.store.complete_external_action(
                provider_id=action.provider_id, operation_id=action.operation_id,
                request_hash=claim_value, state=state, conversation_id=action.conversation_id,
                task_id=case["task_id"], conversation=next_case,
                response=receipt.model_dump(mode="json"),
                before_state=operation_before_state,
            )
            return receipt

    def apply_company_financial_action(
        self, action: CompanyProviderFinancialAction | dict[str, Any],
    ) -> ProviderBankActionReceipt:
        """Reconcile a requested τ bank write only after matching posted payment evidence."""
        action = CompanyProviderFinancialAction.model_validate(action)
        case, state, _task, environment = self._bank_action_context(
            action.provider_id, action.conversation_id, action.company_actor_id,
            action.company_account_reference, action.company_issue_reference,
        )
        operation_before_state = {
            "agent_db": deepcopy(state["agent_db"]), "user_db": deepcopy(state["user_db"]),
        }
        evidence = action.financial_execution
        if (evidence.status != "posted" or evidence.currency != "USD"
                or evidence.account_reference != action.company_account_reference
                or (evidence.source_type == "erp_payment_entry" and evidence.docstatus != 1)):
            raise ExternalProviderError("financial execution evidence is not a posted payment for this company account")
        arguments = dict(action.arguments)
        if Decimal(str(arguments.get("amount", "-1"))) != evidence.amount:
            raise ExternalProviderError("posted payment amount does not match the native Banking transaction")
        user_id = action.mapped_provider_user_id
        if user_id not in environment.tools.db.users.data:
            raise ExternalProviderError("mapped provider user does not exist in the native Banking database")
        account_fields = ("checking_account_id", "credit_card_account_id", "source_account_id", "destination_account_id")
        for field in account_fields:
            account_id = arguments.get(field)
            if account_id is not None and self._bank_account_owner(environment.tools.db, account_id) != user_id:
                raise ExternalProviderError("native account does not belong to the mapped provider user")
        if arguments.get("user_id") is not None and arguments["user_id"] != user_id:
            raise ExternalProviderError("financial action user does not match the mapped provider user")
        if case["company_context"].get("mapped_provider_user_id") not in (None, user_id):
            raise ExternalProviderError("provider conversation is already bound to another native user")

        operation = action.model_dump(mode="json")
        with self.store.locked_provider(action.provider_id):
            prior_operation = self.store.operation(action.provider_id, action.operation_id)
            if prior_operation is not None:
                if prior_operation["request_hash"] != canonical_request_hash(operation):
                    raise ExternalProviderError("operation_id was reused with different financial evidence")
                if prior_operation["status"] == "completed":
                    return ProviderBankActionReceipt.model_validate(prior_operation["response"])
                raise ExternalProviderError("native Banking financial action is uncertain; reconcile the same operation")
            fresh_case, state, _task, environment = self._bank_action_context(
                action.provider_id, action.conversation_id, action.company_actor_id,
                action.company_account_reference, action.company_issue_reference,
            )
            if fresh_case["trajectory"] != case["trajectory"]:
                raise ExternalProviderError("provider conversation changed during financial reconciliation; retry against its current state")
            case = fresh_case
            for field in account_fields:
                account_id = arguments.get(field)
                if account_id is not None and self._bank_account_owner(environment.tools.db, account_id) != user_id:
                    raise ExternalProviderError("native account no longer belongs to the mapped provider user")
            trajectory = list(case["trajectory"])
            matching_call = None
            matching_index = None
            for index, message in enumerate(trajectory):
                if message.get("role") != "assistant":
                    continue
                for call in message.get("tool_calls") or []:
                    if call.get("name") != "call_discoverable_agent_tool":
                        continue
                    call_args = call.get("arguments") or {}
                    if call_args.get("agent_tool_name") != action.native_agent_tool:
                        continue
                    try:
                        native_arguments = json.loads(call_args.get("arguments", "{}"), parse_int=Decimal)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if native_arguments != arguments:
                        continue
                    result_index = next((offset for offset in range(index + 1, len(trajectory))
                                         if trajectory[offset].get("role") == "tool"
                                         and trajectory[offset].get("id") == call.get("id")), None)
                    if result_index is not None and trajectory[result_index].get("execution_status") == "rejected":
                        matching_call, matching_index = call, result_index
            if matching_call is None:
                raise ExternalProviderError("no matching native payment request exists in the provider transcript")
            claim, claim_value = self.store.claim(action.provider_id, action.operation_id, operation)
            if claim != "claimed":
                raise ExternalProviderError("native Banking financial action could not acquire its operation claim")
            from tau2.data_model.message import ToolCall
            original_call = ToolCall.model_validate(matching_call)
            before_db = deepcopy(environment.tools.db.model_dump(mode="json"))
            native_result = environment.get_response(original_call)
            decoded = self._decode_native_tool_result(native_result.content)
            if native_result.error or decoded.startswith("Error:") or decoded.startswith("Failed:"):
                receipt = ProviderBankActionReceipt(
                    provider_id="tau2-banking", operation_id=action.operation_id,
                    conversation_id=action.conversation_id, status="rejected",
                    native_tool=action.native_agent_tool, native_result_hash=_canonical_hash(decoded),
                    native_state_revision=state["state_revision"],
                    source_references=[evidence.source_reference, *action.evidence_references],
                )
                self.store.complete_external_action(
                    provider_id=action.provider_id, operation_id=action.operation_id,
                    request_hash=claim_value, state=state, conversation_id=action.conversation_id,
                    task_id=case["task_id"], conversation=case,
                    response=receipt.model_dump(mode="json"),
                    before_state=operation_before_state,
                )
                return receipt
            environment.sync_tools()
            after_db = environment.tools.db.model_dump(mode="json")
            if (before_db == after_db or not self._bank_payment_effect_matches(
                    action.native_agent_tool, arguments, before_db, after_db, evidence.amount)):
                receipt = ProviderBankActionReceipt(
                    provider_id="tau2-banking", operation_id=action.operation_id,
                    conversation_id=action.conversation_id, status="rejected",
                    native_tool=action.native_agent_tool, native_result_hash=_canonical_hash(decoded),
                    native_state_revision=state["state_revision"],
                    source_references=[evidence.source_reference, *action.evidence_references],
                )
                self.store.complete_external_action(
                    provider_id=action.provider_id, operation_id=action.operation_id,
                    request_hash=claim_value, state=state, conversation_id=action.conversation_id,
                    task_id=case["task_id"], conversation=case,
                    response=receipt.model_dump(mode="json"),
                    before_state=operation_before_state,
                )
                return receipt
            trajectory[matching_index] = _private_json(native_result)
            from tau2.agent.llm_agent import LLMAgentState
            agent_state = LLMAgentState.model_validate(case["agent_state"])
            agent_state.messages = [
                native_result if getattr(message, "id", None) == matching_call.get("id")
                else message
                for message in agent_state.messages
            ]
            agent_db, _ = self._snapshot(environment, action.provider_id)
            revision = _canonical_hash({"agent_db": agent_db, "user_db": None})
            state["agent_db"], state["user_db"] = agent_db, None
            next_case = {
                "agent_state": _private_json(agent_state), "trajectory": trajectory,
                "initialization_status": case["initialization_status"],
                "initialization_evidence": case["initialization_evidence"],
                "company_context": {**case["company_context"], "mapped_provider_user_id": user_id,
                                    "operation_ids": case["company_context"].get("operation_ids", []) + [action.operation_id],
                                    "native_state_revision": revision},
            }
            receipt = ProviderBankActionReceipt(
                provider_id="tau2-banking", operation_id=action.operation_id,
                conversation_id=action.conversation_id, status="applied", native_tool=action.native_agent_tool,
                native_result_hash=_canonical_hash(decoded), native_state_revision=revision,
                source_references=[evidence.source_reference, *action.evidence_references],
            )
            self.store.complete_external_action(
                provider_id=action.provider_id, operation_id=action.operation_id,
                request_hash=claim_value, state=state, conversation_id=action.conversation_id,
                task_id=case["task_id"], conversation=next_case,
                response=receipt.model_dump(mode="json"),
                before_state=operation_before_state,
            )
            return receipt

    def capture_evaluation_snapshot(self, provider_id: str, conversation_id: str) -> ProviderEvaluationSnapshotReceipt:
        """Freeze a private, replayable source-state view for later evaluation."""
        with self.store.locked_provider(provider_id):
            case = self.store.conversation(provider_id, conversation_id)
            if case is None:
                raise ProviderTaskNotFound("native provider conversation was not found")
            baseline = (
                self.store.conversation_baseline(provider_id, conversation_id)
                if provider_id == "tau2-banking"
                else self.store.task_baseline(provider_id, case["task_id"])
            )
            if baseline is None:
                raise RuntimeError("native provider task baseline was not persisted")
            state = self.store.state(provider_id)
            if state is None:
                raise RuntimeError("native provider state was not persisted")
            context = case.get("company_context") or {}
            identity_facts = {
                "provider_id": provider_id, "task_id": case["task_id"],
                "conversation_id": conversation_id,
                "company_id": context.get("company_id"),
                "company_actor_id": context.get("company_actor_id"),
                "company_account_reference": context.get("company_account_reference"),
                "company_issue_reference": context.get("company_issue_reference"),
            }
            identity_fingerprint = _canonical_hash(identity_facts)
            snapshot = {
                "schema_version": 1, "provider_id": provider_id,
                "task_id": case["task_id"], "conversation_id": conversation_id,
                "upstream_commit": TAU2_SOURCE_COMMIT,
                "identity_fingerprint": identity_fingerprint,
                "native_state_revision": state["state_revision"],
                "initialization_status": case["initialization_status"],
                "baseline": baseline, "trajectory": case["trajectory"],
                "native_state": {
                    "agent_db": deepcopy(state["agent_db"]), "user_db": deepcopy(state["user_db"]),
                },
                "operation_ids": context.get("operation_ids", []),
                "operation_changes": self.store.operation_changes(provider_id),
            }
            capture_sha256 = _canonical_hash(snapshot)
            capture_ref = "tau2-provider-evaluation:" + capture_sha256
            self.store.save_evaluation_snapshot(
                capture_ref=capture_ref, capture_sha256=capture_sha256,
                provider_id=provider_id, task_id=case["task_id"],
                conversation_id=conversation_id, upstream_commit=TAU2_SOURCE_COMMIT,
                identity_fingerprint=identity_fingerprint,
                native_state_revision=state["state_revision"], snapshot=snapshot,
            )
            return ProviderEvaluationSnapshotReceipt(
                capture_ref=capture_ref, capture_sha256=capture_sha256,
                provider_id=provider_id, task_id=case["task_id"],
                conversation_id=conversation_id, upstream_commit=TAU2_SOURCE_COMMIT,
                identity_fingerprint=identity_fingerprint,
                native_state_revision=state["state_revision"],
            )

    def _replayed_native_record_digests(self, provider_id: str, task, baseline: dict[str, Any],
                                        trajectory: list[dict[str, Any]]) -> dict[str, str]:
        """Replay the original τ trajectory and expose only native entity digests."""
        from tau2.data_model.message import Message

        replay_state = {
            "agent_db": deepcopy(baseline["agent_db"]),
            "user_db": deepcopy(baseline["user_db"]),
        }
        environment = self._environment(provider_id, replay_state, task)
        initial = task.initial_state
        environment.set_state(
            initialization_data=initial.initialization_data if initial else None,
            initialization_actions=initial.initialization_actions if initial else None,
            message_history=TypeAdapter(list[Message]).validate_python(trajectory),
        )
        agent_db, user_db = self._snapshot(environment, provider_id)
        return native_record_digests(agent_db, user_db)

    def verify_evaluation_snapshot(self, check: ProviderEvaluationSnapshotCheck | dict[str, Any]) -> ProviderEvaluationSnapshotVerification:
        """Verify the saved immutable capture without reading mutable conversation state."""
        check = ProviderEvaluationSnapshotCheck.model_validate(check)
        saved = self.store.evaluation_snapshot(check.capture_ref)
        valid = bool(
            saved
            and saved["capture_sha256"] == check.capture_sha256
            and _canonical_hash(saved["snapshot"]) == check.capture_sha256
            and saved["capture_ref"] == "tau2-provider-evaluation:" + check.capture_sha256
            and saved["snapshot"].get("native_state") is not None
            and self.store._revision(
                saved["snapshot"]["native_state"]["agent_db"],
                saved["snapshot"]["native_state"]["user_db"],
            ) == saved["native_state_revision"]
            and all(saved[key] == saved["snapshot"].get(snapshot_key) for key, snapshot_key in (
                ("provider_id", "provider_id"), ("task_id", "task_id"),
                ("conversation_id", "conversation_id"), ("upstream_commit", "upstream_commit"),
                ("identity_fingerprint", "identity_fingerprint"),
                ("native_state_revision", "native_state_revision"),
            ))
        )
        return ProviderEvaluationSnapshotVerification(
            capture_ref=check.capture_ref, capture_sha256=check.capture_sha256, valid=valid,
        )

    def evaluate_captured(self, check: ProviderEvaluationSnapshotCheck | dict[str, Any]) -> ProviderEvaluationResult:
        """Run the upstream evaluator only on the exact immutable captured input."""
        check = ProviderEvaluationSnapshotCheck.model_validate(check)
        verified = self.verify_evaluation_snapshot(check)
        if not verified.valid:
            raise ExternalProviderError("native evaluation snapshot is missing or its hash does not match")
        row = self.store.evaluation_snapshot(check.capture_ref)
        if row is None:
            raise ExternalProviderError("native evaluation snapshot disappeared after verification")
        snapshot = row["snapshot"]
        provider_id, conversation_id, task_id = (
            snapshot["provider_id"], snapshot["conversation_id"], snapshot["task_id"]
        )
        if row["upstream_commit"] != TAU2_SOURCE_COMMIT or snapshot["upstream_commit"] != TAU2_SOURCE_COMMIT:
            raise ExternalProviderError("native evaluation snapshot is pinned to a different τ source revision")
        task = self._load_task(provider_id, task_id)
        if snapshot["initialization_status"] == "conflict":
            return ProviderEvaluationResult(
                provider_id=provider_id, conversation_id=conversation_id, task_id=task_id,
                evaluation_mode="company_adapted_native_tau_criteria", status="inconclusive",
                reason_code="shared_provider_initialization_conflict",
                native_state_revision=snapshot["native_state_revision"],
            )
        baseline = snapshot["baseline"]
        from tau2.data_model.message import Message
        from tau2.data_model.simulation import SimulationRun, TerminationReason
        from tau2.domains.airline.data_model import FlightDB
        from tau2.domains.telecom.data_model import TelecomDB
        from tau2.domains.telecom.user_data_model import TelecomUserDB
        from tau2.evaluator.evaluator import EvaluationType, evaluate_simulation
        from tau2.utils.utils import get_now

        trajectory = TypeAdapter(list[Message]).validate_python(snapshot["trajectory"])
        start_time = get_now()
        domain = self._domain(provider_id)
        if domain == "airline":
            env_kwargs = {"db": FlightDB.model_validate(baseline["agent_db"])}
        elif domain == "telecom":
            env_kwargs = {
                "db": TelecomDB.model_validate(baseline["agent_db"]),
                "user_db": TelecomUserDB.model_validate(baseline["user_db"]),
                "policy_type": "manual",
            }
        else:
            from tau2.domains.banking_knowledge.data_model import KnowledgeBase, TransactionalDB
            directory = self.runtime_root / "data/tau2/domains/banking_knowledge"
            env_kwargs = {
                "db": TransactionalDB.model_validate(baseline["agent_db"]),
                "knowledge_base": KnowledgeBase.load(str(directory / "documents")),
                "retrieval_variant": "bm25_grep",
                "task": task,
                "read_log_allowlist": _banking_read_log_allowlist(task),
            }
        simulation = SimulationRun(
            id=conversation_id, task_id=task_id, start_time=start_time,
            end_time=get_now(), duration=0, termination_reason=TerminationReason.USER_STOP,
            messages=trajectory, mode="half_duplex",
        )
        if snapshot.get("native_state") is not None:
            expected_records = self._replayed_native_record_digests(
                provider_id, task, baseline, snapshot["trajectory"],
            )
            baseline_records = native_record_digests(baseline["agent_db"], baseline["user_db"])
            actual_records = native_record_digests(
                snapshot["native_state"]["agent_db"], snapshot["native_state"]["user_db"],
            )
            expected_refs = {
                ref for ref in baseline_records.keys() | expected_records.keys()
                if baseline_records.get(ref) != expected_records.get(ref)
            }
            task_refs = _task_native_record_refs(
                task, baseline["agent_db"], baseline["user_db"],
            )
            relevant_refs = expected_refs | task_refs
            own_operations = set(snapshot.get("operation_ids", []))
            external_changes = {
                ref
                for operation in snapshot.get("operation_changes", [])
                if operation["conversation_id"] != conversation_id
                and operation["operation_id"] not in own_operations
                for ref in operation["changed_records"]
            }
            shared_refs = sorted(relevant_refs & external_changes)
            mismatched_refs = sorted(
                ref for ref in relevant_refs
                if actual_records.get(ref) != expected_records.get(ref)
            )
            # A read-only/zero-write golden trajectory can still depend on
            # records another company case changed. If the task offers no
            # exact record identities with which to scope that state, report
            # attribution as inconclusive instead of turning an ambiguous
            # zero score into an assistant failure.
            scope_unknown = bool(external_changes) and not relevant_refs
            if shared_refs or mismatched_refs or scope_unknown:
                reason = ("shared_native_record_changed" if shared_refs else
                          "captured_native_state_not_reconciled" if mismatched_refs else
                          "shared_native_state_scope_unknown")
                return ProviderEvaluationResult(
                    provider_id=provider_id, conversation_id=conversation_id, task_id=task_id,
                    evaluation_mode="company_adapted_native_tau_criteria", status="inconclusive",
                    reason_code=reason,
                    native_state_revision=snapshot["native_state_revision"],
                )
        from tau2.data_model.semantic_communication import (
            CommunicateScoring, SemanticCommunicateSettings,
        )
        from tau2.data_model.tasks import RewardType
        from servers.simulation.tau2_retail import (
            codex_nl_assertion_context, validate_nl_assertion_coverage,
        )

        # Match τ's runner default (semantic communicate scoring), routing its
        # judge to the already configured Codex CLI provider. The installed
        # evaluator requires this keyword even when the task has no COMMUNICATE
        # component. Only provider/model arguments change; the pinned prompt,
        # native Task criteria, and evaluator scales stay intact.
        # τ's default reasoning_effort argument is unsupported by the registered
        # Codex CLI model, so preserve the semantic judge with empty model_args.
        communicate_scoring = CommunicateScoring(
            judge=SemanticCommunicateSettings(model=PROVIDER_LLM, model_args={}),
        )
        needs_nl_judge = bool(
            task.evaluation_criteria
            and RewardType.NL_ASSERTION in task.evaluation_criteria.reward_basis
            and task.evaluation_criteria.nl_assertions
        )
        nl_judge_context = codex_nl_assertion_context() if needs_nl_judge else nullcontext()
        with nl_judge_context:
            reward = evaluate_simulation(
                simulation=simulation, task=task, evaluation_type=EvaluationType.ALL,
                solo_mode=False, domain=domain, env_kwargs=env_kwargs,
                communicate_scoring=communicate_scoring,
            )
        if needs_nl_judge:
            validate_nl_assertion_coverage(task, reward)
        return ProviderEvaluationResult(
            provider_id=provider_id, conversation_id=conversation_id, task_id=task_id,
            evaluation_mode="company_adapted_native_tau_criteria", status="completed",
            native_reward_info=reward.model_dump(mode="json"),
            native_state_revision=snapshot["native_state_revision"],
        )

    def evaluate_conversation(self, provider_id: str, conversation_id: str) -> ProviderEvaluationResult:
        """Capture then evaluate a stable native τ conversation snapshot."""
        capture = self.capture_evaluation_snapshot(provider_id, conversation_id)
        return self.evaluate_captured({
            "capture_ref": capture.capture_ref, "capture_sha256": capture.capture_sha256,
        })

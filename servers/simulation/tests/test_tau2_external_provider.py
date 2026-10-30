"""Synthetic native-contract tests; they do not run a benchmark task or call an LLM."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

from servers.simulation.provider_contracts import (
    CompanyPaymentEvidence, CompanyProviderFinancialAction, CompanyProviderPortalAction,
    ProviderEvidenceReference, ProviderFinancialExecutionEvidence, ProviderRequest,
    ProviderCasePrepareRequest, ProviderEvaluationSnapshotCheck,
)
from servers.simulation.provider_store import NativeProviderStore, native_record_digests
from servers.simulation.tau2_external_provider import (
    PROVIDER_DOMAIN_FILES,
    TAU2_SOURCE_COMMIT,
    Tau2ExternalProvider,
    ExternalProviderError,
    _banking_read_log_allowlist,
    _banking_patch_conflicts,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RUNTIME = REPOSITORY_ROOT / "servers/simulation/tau2_actor_runtime"
DATA = RUNTIME / "data"
os.environ["TAU2_DATA_DIR"] = str(DATA)

try:
    from tau2.data_model.message import AssistantMessage, ToolCall
    from tau2.domains.telecom.data_model import BillStatus
except ImportError:  # The native checks run in the isolated τ runtime environment.
    AssistantMessage = None
    ToolCall = None
    BillStatus = None


@unittest.skipIf(AssistantMessage is None, "run with tau2_actor_runtime/.venv")
class NativeExternalProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "provider.sqlite"
        self.service = Tau2ExternalProvider(NativeProviderStore(self.database))

    def test_banking_native_loader_and_environment_use_upstream_bm25_and_tool_boundary(self):
        from tau2.domains.banking_knowledge.environment import get_tasks

        manifest = self.service._verify_assets()
        bank_meta = next(row for row in manifest["providers"] if row["provider_id"] == "tau2-banking")
        self.assertEqual(bank_meta["task_count"], 97)
        tasks = get_tasks(task_split_name=None)
        self.assertEqual(len(tasks), 97)
        self.assertEqual(len({task.id for task in tasks}), 97)
        discoverable_read = next(
            (task, action) for task in tasks if task.evaluation_criteria
            for action in (task.evaluation_criteria.actions or [])
            if action.requestor == "assistant"
            and action.name == "call_discoverable_agent_tool"
        )
        self.assertIn(discoverable_read[1].arguments["agent_tool_name"],
                      _banking_read_log_allowlist(discoverable_read[0]))
        initialized_data = [task for task in tasks if task.initial_state and task.initial_state.initialization_data]
        self.assertEqual(len(initialized_data), 53)
        baseline, _ = self.service._base_databases("tau2-banking")
        self.assertTrue(all(
            not _banking_patch_conflicts(
                baseline, baseline,
                task.initial_state.initialization_data.model_dump(mode="json", exclude_none=True),
            ) for task in initialized_data
        ))
        task = self.service._load_task("tau2-banking", "task_001")
        state = self.service._bootstrap("tau2-banking")
        environment = self.service._environment("tau2-banking", state, task)
        self.assertEqual(environment.domain_name, "banking_knowledge")
        self.assertTrue(environment.tools.has_tool("KB_search"))
        self.assertNotIn("apply_for_credit_card", {tool.name for tool in environment.get_tools()})
        self.assertTrue(environment.user_tools.has_tool("apply_for_credit_card"))
        self.assertFalse(environment.solo_mode)

    def test_public_provider_catalogue_uses_customer_requests_and_hides_evaluator_intent(self):
        catalog = self.service.public_task_catalog()
        self.assertEqual(catalog.upstream_commit, TAU2_SOURCE_COMMIT)
        self.assertEqual(len(catalog.tasks), 2432)
        self.assertEqual(
            {provider: sum(task.provider_id == provider for task in catalog.tasks)
             for provider in PROVIDER_DOMAIN_FILES},
            {"tau2-airline": 50, "tau2-telecom": 2285, "tau2-banking": 97},
        )
        telecom_tasks = [task for task in catalog.tasks if task.provider_id == "tau2-telecom"]
        self.assertEqual(sum(task.classification == "financial" for task in telecom_tasks), 1128)
        self.assertEqual(sum(task.classification == "operational" for task in telecom_tasks), 1157)
        self.assertTrue(all(task.task_id and task.description for task in catalog.tasks))
        public = catalog.model_dump(mode="json")
        self.assertNotIn("evaluation_criteria", str(public))
        self.assertNotIn("user_scenario", str(public))
        self.assertNotIn("purpose", str(public))
        self.assertNotIn("relevant_policies", str(public))
        self.assertNotIn("Testing that agent refuses", str(public))
        first_airline = next(row for row in catalog.tasks
                             if row.provider_id == "tau2-airline" and row.task_id == "0")
        self.assertEqual(first_airline.description,
                         "You want to cancel reservation EHGLP3. \n\nIt may be more than 24 hours after booking, but it is ok because you were out of town for that time.")
        first_bank = next(row for row in catalog.tasks
                          if row.provider_id == "tau2-banking" and row.task_id == "task_001")
        self.assertEqual(first_bank.title, "Everyday card with high cash back")
        self.assertEqual(first_bank.role_fit, "personal_customer")
        self.assertIn("credit card", first_bank.description.lower())
        self.assertTrue(first_bank.requested_outcome)
        self.assertNotIn("private_factual_context", str(public))
        self.assertNotIn("Sarah Bosch", str(public))

    def test_banking_briefs_cover_all_native_tasks_with_exact_private_source_spans(self):
        from servers.simulation.tau2_banking_briefs import _verified_briefs, private_factual_context
        source_path = RUNTIME / "data/tau2/domains/banking_knowledge/tasks.json"
        source_rows = {row["id"]: row for row in json.loads(source_path.read_text())}

        catalog = [row for row in self.service.public_task_catalog().tasks
                   if row.provider_id == "tau2-banking"]
        self.assertEqual(len(catalog), 97)
        self.assertEqual(len({row.task_id for row in catalog}), 97)
        self.assertGreaterEqual(len({row.description for row in catalog}), 90)
        briefs, _ = _verified_briefs()
        review_rows = {row["task_id"]: row for row in briefs["tasks"]}
        self.assertEqual({row.role_fit for row in catalog},
                         {"personal_customer", "business_customer", "mixed_customer"})
        self.assertEqual({role: sum(row.role_fit == role for row in catalog) for role in
                          ("personal_customer", "mixed_customer", "business_customer")},
                         {"personal_customer": 70, "mixed_customer": 20, "business_customer": 7})
        # The private brief exposes selected source facts as exact, independently
        # verifiable spans, while public cards carry only curated summaries.
        for entry in catalog:
            facts = private_factual_context(entry.task_id)
            self.assertTrue(facts, entry.task_id)
            self.assertTrue(all(fact.source_json_pointer == "/user_scenario/instructions"
                                for fact in facts))
            self.assertTrue(all(fact.end_char > fact.start_char for fact in facts))
            self.assertNotIn("evaluation_criteria", str(facts))
            source = source_rows[entry.task_id]["user_scenario"]["instructions"]
            self.assertTrue(all(
                fact.text == source[fact.start_char:fact.end_char] for fact in facts
            ))
            request_span = review_rows[entry.task_id]["review_source_request"]
            self.assertEqual(request_span["json_pointer"], "/user_scenario/instructions")
            self.assertEqual(request_span["source_task_sha256"],
                             review_rows[entry.task_id]["source_task_sha256"])
            self.assertEqual(request_span["excerpt"],
                             source[request_span["start_char"]:request_span["end_char"]])
            self.assertNotRegex(request_span["excerpt"],
                                r"(?i)(?:\b\d{3}-\d{3}-\d{4}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\bCVV\b|\bPIN\s*:)" )
        for task_id, prohibited in {
            "task_008": ("become MORE frustrated", "Use phrases like", "must be refused"),
            "task_068": ("I trust you to figure out the best combo", "KEY:**"),
            "task_081": ("Request 3:", "###STOP###", "FIRST TURN:"),
        }.items():
            facts = "\n".join(fact.text for fact in private_factual_context(task_id))
            for phrase in prohibited:
                self.assertNotIn(phrase, facts)
        self.assertNotIn("CVV", review_rows["task_077"]["review_source_request"]["excerpt"])
        public_by_id = {row.task_id: row for row in catalog}
        self.assertEqual(public_by_id["task_012"].role_fit, "mixed_customer")
        self.assertEqual(public_by_id["task_023"].role_fit, "mixed_customer")
        self.assertEqual(public_by_id["task_026"].role_fit, "mixed_customer")
        self.assertEqual(public_by_id["task_021"].role_fit, "mixed_customer")
        self.assertEqual(public_by_id["task_045"].role_fit, "business_customer")
        self.assertEqual(public_by_id["task_070"].role_fit, "mixed_customer")
        self.assertEqual(public_by_id["task_071"].role_fit, "mixed_customer")
        public_text = " ".join(f"{row.title} {row.description} {row.requested_outcome or ''}"
                                for row in catalog)
        self.assertNotRegex(public_text, r"(?i)(?:paired case|source wording|task-linked customer records)")
        task_007_span = review_rows["task_007"]["review_source_request"]["excerpt"]
        self.assertIn("sign-up bonus", task_007_span)
        self.assertIn("lowest annual fee", task_007_span)
        task_008 = public_by_id["task_008"]
        self.assertEqual(task_008.role_fit, "personal_customer")

    def test_prepare_initializes_native_account_and_customer_forms_without_generation(self):
        from tau2.agent.llm_agent import LLMAgentState

        calls = {"init": 0}

        class NativeStateOnlyAgent:
            def get_init_state(self, history):
                calls["init"] += 1
                return LLMAgentState(system_messages=[], messages=history)

        with patch.object(self.service, "_create_agent", return_value=NativeStateOnlyAgent()):
            request = ProviderCasePrepareRequest(
                provider_id="tau2-banking", task_id="task_001",
                conversation_id="synthetic-prepare-no-generation",
                company_actor_id="employee:fixture", company_account_reference="company-account:fixture",
                company_issue_reference="company-issue:fixture",
            )
            prepared = self.service.prepare_case(request)
            self.assertEqual(prepared.status, "prepared")
            self.assertEqual(prepared.initialization_status, "applied")
            self.assertIsNone(prepared.account)  # No provider account is fabricated for a new application.
            card_form = next(form for form in prepared.forms if form.source_tool_name == "apply_for_credit_card")
            self.assertIn("Gold Rewards Card", card_form.argument_schema["properties"]["card_type"]["enum"])
            self.assertIn("credit card", prepared.business_description.lower())
            self.assertEqual(prepared.requested_outcome,
                             "Compare personal everyday-spend cards by cash-back return and annual fee, then recommend the best fit.")
            self.assertTrue(prepared.private_factual_context)
            self.assertTrue(all(fact.source_task_sha256 and fact.source_json_pointer
                                for fact in prepared.private_factual_context))
            self.assertNotIn("evaluation_criteria", prepared.model_dump_json())
            replay = self.service.prepare_case(request)
            self.assertEqual(replay.status, "already_prepared")
            self.assertEqual(calls["init"], 1)
            self.assertFalse(hasattr(NativeStateOnlyAgent, "generate_next_message"))
            airline = self.service.prepare_case(ProviderCasePrepareRequest(
                provider_id="tau2-airline", task_id="0", conversation_id="synthetic-airline-view",
                company_actor_id="employee:fixture", company_account_reference="company-account:airline",
                company_issue_reference="company-issue:airline",
            ))
            self.assertEqual(airline.account.display_name, "Emma Kim")
            airline_task = self.service._load_task("tau2-airline", "0")
            self.assertIn(airline_task.user_scenario.instructions.known_info,
                          airline.business_description)
            self.assertNotIn(airline_task.user_scenario.instructions.task_instructions,
                             airline.business_description)
            self.assertTrue(any(row.entity_type == "Reservation" for row in airline.account.records))
            telecom = self.service.prepare_case(ProviderCasePrepareRequest(
                provider_id="tau2-telecom",
                task_id="[mobile_data_issue]data_usage_exceeded[PERSONA:Easy]",
                conversation_id="synthetic-telecom-view", company_actor_id="employee:fixture",
                company_account_reference="company-account:telecom", company_issue_reference="company-issue:telecom",
            ))
            self.assertEqual(telecom.account.display_name, "John Smith")
            self.assertTrue(any(row.entity_type == "Bill" for row in telecom.account.records))
            bank_account = self.service.prepare_case(ProviderCasePrepareRequest(
                provider_id="tau2-banking", task_id="task_062", conversation_id="synthetic-bank-view",
                company_actor_id="employee:fixture", company_account_reference="company-account:bank-view",
                company_issue_reference="company-issue:bank-view",
            ))
            self.assertEqual(bank_account.account.display_name, "Jordan Chen")
            self.assertTrue(any(row.entity_type == "accounts" for row in bank_account.account.records))
            self.assertIn("close", bank_account.business_description.lower())

    def test_full_telecom_catalog_includes_preparable_operational_source_task(self):
        from tau2.agent.llm_agent import LLMAgentState
        from tau2.data_model.tasks import Task

        raw_tasks = json.loads((RUNTIME / "data/tau2/domains/telecom/tasks.json").read_text())
        raw = next(row for row in raw_tasks if self.service._public_task_classification(
            "tau2-telecom", Task.model_validate(row)
        ) == "operational")
        task = Task.model_validate(raw)

        class NativeStateOnlyAgent:
            def get_init_state(self, history):
                return LLMAgentState(system_messages=[], messages=history)

        request = ProviderCasePrepareRequest(
            provider_id="tau2-telecom", task_id=task.id,
            conversation_id="synthetic-telecom-operational-prepare",
            company_actor_id="employee:fixture", company_account_reference="company-account:telecom",
            company_issue_reference="company-issue:telecom",
        )
        with patch.object(self.service, "_create_agent", return_value=NativeStateOnlyAgent()):
            prepared = self.service.prepare_case(request)
        self.assertEqual(prepared.status, "prepared")
        self.assertEqual(prepared.initialization_status, "applied")
        self.assertEqual(prepared.business_description,
                         task.user_scenario.instructions.reason_for_call + "\n\nCustomer-supplied details: "
                         + task.user_scenario.instructions.known_info)
        for private_text in (task.user_scenario.instructions.unknown_info,
                             task.user_scenario.instructions.task_instructions):
            if private_text:
                self.assertNotIn(private_text, prepared.business_description)
        self.assertGreater(len(prepared.forms), 0)
        self.assertNotIn("evaluation_criteria", prepared.model_dump_json())

    def test_telecom_customer_forms_follow_native_none_and_empty_tool_semantics(self):
        task = self.service._load_task(
            "tau2-telecom", "[mobile_data_issue]data_usage_exceeded[PERSONA:Easy]",
        )
        state = self.service._bootstrap("tau2-telecom")
        environment = self.service._environment("tau2-telecom", state, task)
        all_forms = self.service._native_portal_forms(task, environment)
        self.assertIn("make_payment", {form.source_tool_name for form in all_forms})
        empty_task = task.model_copy(update={"user_tools": []})
        self.assertEqual(self.service._native_portal_forms(empty_task, environment), [])

    def test_telecom_portal_payment_uses_native_customer_tool_without_erp_preposting(self):
        provider_id = "tau2-telecom"
        task_id = "[service_issue]overdue_bill_suspension[PERSONA:Easy]"
        task = self.service._load_task(provider_id, task_id)
        payment_request_action = next(
            action for action in task.evaluation_criteria.actions
            if action.requestor == "assistant" and action.name == "send_payment_request"
        )
        from tau2.data_model.message import AssistantMessage, ToolCall
        native_reply = [
            AssistantMessage(role="assistant", tool_calls=[ToolCall(
                id="native-portal-payment-request", name="send_payment_request",
                arguments=payment_request_action.arguments, requestor="assistant",
            )]),
            AssistantMessage(role="assistant", content="Payment request sent.", tool_calls=None),
        ]
        request = self.request(
            task_id, operation_id="synthetic-telecom-portal-message",
            conversation_id="synthetic-telecom-portal-thread",
            message_id="<synthetic-telecom-portal@example.test>",
        )
        with patch("tau2.agent.llm_agent.generate", side_effect=native_reply):
            self.service.handle(request)
        account = self.service._portal_account_view(
            provider_id, task,
            self.service._environment(provider_id, self.service.store.state(provider_id), task),
        )
        self.assertIsNotNone(account)
        customer_id = account.source_id
        state = self.service.store.state(provider_id)
        environment = self.service._environment(provider_id, state, task)
        payment_request = environment.user_tools.db.surroundings.payment_request
        self.assertIsNotNone(payment_request)
        bill = next(row for row in environment.tools.db.bills if row.bill_id == payment_request.bill_id)
        self.assertEqual(bill.status, BillStatus.AWAITING_PAYMENT)
        action = CompanyProviderPortalAction(
            provider_id=provider_id, operation_id="synthetic-telecom-portal-payment",
            conversation_id=request.conversation_id, company_actor_id=request.company_actor_id,
            company_account_reference=request.company_account_reference,
            company_issue_reference=request.company_issue_reference,
            mapped_provider_user_id=customer_id, native_user_tool="make_payment", arguments={},
            portal_event="portal_action_confirmed",
            evidence_references=[ProviderEvidenceReference(
                source="company_provider_portal", reference="portal-payment:synthetic-1",
            )], correlation_id="synthetic-telecom-portal-payment",
        )
        receipt = self.service.apply_company_portal_action(action)
        self.assertEqual(receipt.status, "applied")
        self.assertEqual(receipt.provider_id, provider_id)
        after = self.service._environment(provider_id, self.service.store.state(provider_id), task)
        paid_bill = next(row for row in after.tools.db.bills if row.bill_id == payment_request.bill_id)
        self.assertEqual(paid_bill.status, BillStatus.PAID)
        self.assertIsNone(after.user_tools.db.surroundings.payment_request)

    def test_native_banking_agent_tool_executes_without_erp_preposting(self):
        from tau2.agent.llm_agent import LLMAgentState
        from tau2.data_model.message import AssistantMessage, ToolCall

        task = self.service._load_task("tau2-banking", "task_051")
        source_action = next(action for action in task.evaluation_criteria.actions
                             if action.name == "call_discoverable_agent_tool"
                             and action.arguments.get("agent_tool_name") == "pay_credit_card_from_checking_9182")
        native_arguments = json.loads(source_action.arguments["arguments"])
        unlock = ToolCall(id="synthetic-unlock", name="unlock_discoverable_agent_tool",
                          arguments={"agent_tool_name": "pay_credit_card_from_checking_9182"},
                          requestor="assistant")
        payment = ToolCall(id="synthetic-native-payment", name="call_discoverable_agent_tool",
                           arguments={"agent_tool_name": "pay_credit_card_from_checking_9182",
                                      "arguments": json.dumps(native_arguments)}, requestor="assistant")

        class AuthorizedNativeAgent:
            def __init__(self):
                self.turn = 0

            def get_init_state(self, history):
                return LLMAgentState(system_messages=[], messages=history)

            def generate_next_message(self, _message, state):
                self.turn += 1
                if self.turn == 1:
                    return AssistantMessage(role="assistant", tool_calls=[unlock]), state
                if self.turn == 2:
                    return AssistantMessage(role="assistant", tool_calls=[payment]), state
                return AssistantMessage(role="assistant", content="The payment is complete."), state

        agent = AuthorizedNativeAgent()
        with patch.object(self.service, "_create_agent", return_value=agent):
            response = self.service.handle(ProviderRequest(
                provider_id="tau2-banking", task_id=task.id, operation_id="synthetic-native-bank-payment",
                conversation_id="synthetic-native-bank-payment-case", company_actor_id="employee:fixture",
                company_account_reference="company-account:fixture", company_issue_reference="company-issue:fixture",
                message_id="<synthetic-native-bank-payment@example.test>",
                subject="Bank account support", body="Please help with this account payment.",
            ))
        self.assertEqual(response.status, "completed")
        case = self.service.store.conversation("tau2-banking", "synthetic-native-bank-payment-case")
        result = next(message for message in case["trajectory"]
                      if message.get("id") == payment.id and message.get("role") == "tool")
        self.assertFalse(result["error"])
        self.assertTrue(response.evidence)

    def test_native_evaluation_uses_immutable_capture_without_rereading_conversation(self):
        from tau2.agent.llm_agent import LLMAgentState

        class NativeStateOnlyAgent:
            def get_init_state(self, history):
                return LLMAgentState(system_messages=[], messages=history)

        with patch.object(self.service, "_create_agent", return_value=NativeStateOnlyAgent()):
            prepared = self.service.prepare_case(ProviderCasePrepareRequest(
                provider_id="tau2-airline", task_id="0",
                conversation_id="synthetic-evaluation-capture",
                company_actor_id="employee:fixture", company_account_reference="company-account:fixture",
                company_issue_reference="company-issue:fixture",
            ))
        self.assertEqual(prepared.account.display_name, "Emma Kim")
        capture = self.service.capture_evaluation_snapshot("tau2-airline", prepared.conversation_id)
        check = ProviderEvaluationSnapshotCheck(
            capture_ref=capture.capture_ref, capture_sha256=capture.capture_sha256,
        )
        self.assertTrue(self.service.verify_evaluation_snapshot(check).valid)
        # A later shared provider write cannot alter this frozen input or make
        # evaluation reread the mutable conversation.
        with self.service.store._connection() as db:
            db.execute(
                "UPDATE native_provider_conversations SET trajectory_json='[]' "
                "WHERE provider_id=? AND conversation_id=?",
                ("tau2-airline", prepared.conversation_id),
            )

        class Reward:
            reward = 0.0

            @staticmethod
            def model_dump(mode="json"):
                return {"reward": 0.0}

        with patch.object(self.service.store, "conversation", side_effect=AssertionError("mutable conversation reread")), \
             patch("tau2.evaluator.evaluator.evaluate_simulation", return_value=Reward()) as evaluate:
            result = self.service.evaluate_captured(check)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.native_state_revision, capture.native_state_revision)
        saved_capture = self.service.store.evaluation_snapshot(capture.capture_ref)
        self.assertEqual(
            len(evaluate.call_args.kwargs["simulation"].messages),
            len(saved_capture["snapshot"]["trajectory"]),
        )
        self.assertTrue(self.service.verify_evaluation_snapshot(check).valid)
        wrong_hash = check.model_copy(update={"capture_sha256": "0" * 64})
        with self.assertRaises(ExternalProviderError):
            self.service.evaluate_captured(wrong_hash)

    def test_captured_evaluation_uses_real_tau_evaluator_and_codex_nl_context(self):
        """Exercise the pinned evaluator signature and NL evaluator without an LLM call."""
        from types import SimpleNamespace
        from tau2.data_model.tasks import EvaluationCriteria, RewardType, Task, UserScenario
        from tau2.evaluator import evaluator_nl_assertions

        source_task = self.service._load_task("tau2-airline", "0")
        prepared = self.service.prepare_case(ProviderCasePrepareRequest(
            provider_id="tau2-airline", task_id="0",
            conversation_id="synthetic-native-evaluator-signature",
            company_actor_id="employee:fixture", company_account_reference="company-account:fixture",
            company_issue_reference="company-issue:fixture",
        ))
        capture = self.service.capture_evaluation_snapshot("tau2-airline", prepared.conversation_id)
        check = ProviderEvaluationSnapshotCheck(
            capture_ref=capture.capture_ref, capture_sha256=capture.capture_sha256,
        )
        criteria = EvaluationCriteria(
            communicate_info=["the request was received"],
            nl_assertions=[
                "The agent acknowledges the customer's request.",
                "The agent explains the next step.",
            ],
            reward_basis=[RewardType.COMMUNICATE, RewardType.NL_ASSERTION],
        )
        synthetic_task = Task(
            id="synthetic-native-evaluator-signature",
            user_scenario=UserScenario(instructions="Synthetic evaluator contract fixture."),
            evaluation_criteria=criteria,
        )
        self.assertEqual(source_task.initial_state, None)
        self.assertEqual(synthetic_task.evaluation_criteria.model_dump(), criteria.model_dump())
        original_nl_model = evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS
        original_nl_args = evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS
        def controlled_judge(reply):
            def generate(*, call_name, **kwargs):
                response = reply if call_name == "nl_assertions_eval" else {
                    "reasoning": "Controlled fixture response.", "met": True,
                }
                return SimpleNamespace(content=json.dumps(response))
            return generate

        valid_reply = {"results": [{
            "expectedOutcome": assertion, "reasoning": "Controlled fixture response.", "metExpectation": True,
        } for assertion in criteria.nl_assertions]}
        native_generate = patch(
            "tau2.evaluator.evaluator_nl_assertions.generate", side_effect=controlled_judge(valid_reply),
        )
        communicate_generate = patch(
            "tau2.evaluator.evaluator_communicate.generate",
            side_effect=controlled_judge(valid_reply),
        )
        with patch.object(self.service, "_load_task", return_value=synthetic_task), native_generate as generate:
            # Evaluate the actual native evaluator implementation. Only its
            # LLM transport boundary is controlled; no model or episode runs.
            with communicate_generate as communicate:
                result = self.service.evaluate_captured(check)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.native_reward_info["reward_basis"], ["COMMUNICATE", "NL_ASSERTION"])
        self.assertEqual(result.native_reward_info["reward"], 1.0)
        self.assertEqual(result.native_reward_info["reward_breakdown"], {
            "COMMUNICATE": 1.0, "NL_ASSERTION": 1.0,
        })
        generate.assert_called_once()
        self.assertEqual(generate.call_args.kwargs["model"], "tac_codex/codex-cli")
        self.assertEqual(generate.call_args.kwargs["call_name"], "nl_assertions_eval")
        communicate.assert_called_once()
        scoring = result.native_reward_info["info"]["communicate"]["communicate_scoring"]
        self.assertEqual(scoring["scorer"], "semantic")
        self.assertEqual(scoring["judge"]["model"], "tac_codex/codex-cli")
        self.assertEqual(scoring["judge"]["prompt_version"], "communicate-semantic-v1")
        self.assertEqual(evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS, original_nl_model)
        self.assertEqual(evaluator_nl_assertions.DEFAULT_LLM_NL_ASSERTIONS_ARGS, original_nl_args)

        reordered_reply = {"results": list(reversed(valid_reply["results"]))}
        with patch.object(self.service, "_load_task", return_value=synthetic_task), \
             patch("tau2.evaluator.evaluator_nl_assertions.generate",
                   side_effect=controlled_judge(reordered_reply)), \
             patch("tau2.evaluator.evaluator_communicate.generate",
                   side_effect=controlled_judge(reordered_reply)):
            reordered = self.service.evaluate_captured(check)
        self.assertEqual(reordered.status, "completed")
        self.assertEqual(reordered.native_reward_info["reward"], 1.0)
        self.assertEqual(
            [check["nl_assertion"] for check in reordered.native_reward_info["nl_assertions"]],
            list(reversed(criteria.nl_assertions)),
        )

        optional_nl_task = synthetic_task.model_copy(update={
            "evaluation_criteria": criteria.model_copy(update={
                "reward_basis": [RewardType.COMMUNICATE],
            }),
        })
        with patch.object(self.service, "_load_task", return_value=optional_nl_task), \
             patch("tau2.evaluator.evaluator_nl_assertions.generate") as nl_generate, \
             patch("tau2.evaluator.evaluator_communicate.generate",
                   side_effect=controlled_judge(valid_reply)) as communicate_only:
            optional = self.service.evaluate_captured(check)
        self.assertEqual(optional.status, "completed")
        self.assertEqual(optional.native_reward_info["reward_basis"], ["COMMUNICATE"])
        self.assertIsNone(optional.native_reward_info["nl_assertions"])
        nl_generate.assert_not_called()
        communicate_only.assert_called_once()

        invalid_replies = (
            {},
            {"results": [valid_reply["results"][0]]},
            {"results": [valid_reply["results"][0], valid_reply["results"][0]]},
        )
        for invalid_reply in invalid_replies:
            with self.subTest(invalid_reply=invalid_reply):
                with patch.object(self.service, "_load_task", return_value=synthetic_task), \
                     patch("tau2.evaluator.evaluator_nl_assertions.generate",
                           side_effect=controlled_judge(invalid_reply)), \
                     patch("tau2.evaluator.evaluator_communicate.generate",
                           side_effect=controlled_judge(invalid_reply)):
                    with self.assertRaisesRegex(RuntimeError, "incomplete criterion coverage"):
                        self.service.evaluate_captured(check)

        with patch.object(self.service, "_load_task", return_value=synthetic_task), \
             patch("tau2.evaluator.evaluator_nl_assertions.generate", side_effect=TimeoutError("controlled")), \
             patch("tau2.evaluator.evaluator_communicate.generate", side_effect=controlled_judge(valid_reply)):
            with self.assertRaisesRegex(TimeoutError, "controlled"):
                self.service.evaluate_captured(check)

    def test_native_operation_change_log_attributes_source_records_by_id(self):
        store = self.service.store
        before_db = {"accounts": {"data": {
            "checking-1": {"account_id": "checking-1", "balance": 100},
            "checking-2": {"account_id": "checking-2", "balance": 100},
        }}}
        state = store.bootstrap("tau2-banking", TAU2_SOURCE_COMMIT, before_db, None)
        before = {"agent_db": json.loads(json.dumps(state["agent_db"])), "user_db": None}
        state["agent_db"]["accounts"]["data"]["checking-1"]["balance"] = 90
        conversation = {
            "agent_state": {}, "trajectory": [], "initialization_status": "applied",
            "initialization_evidence": {}, "company_context": {},
        }
        operation = {"change": "checking-1"}
        claim, claim_hash = store.claim("tau2-banking", "synthetic-change", operation)
        self.assertEqual(claim, "claimed")
        store.commit_operation(
            provider_id="tau2-banking", operation_id="synthetic-change", request_hash=claim_hash,
            state=state, conversation_id="synthetic-change-case", task_id="synthetic-task",
            conversation=conversation, baseline=None, response={"status": "done"}, before_state=before,
        )
        changes = store.operation_changes("tau2-banking")
        self.assertEqual(len(changes), 1)
        self.assertEqual(set(changes[0]["changed_records"]), {"agent/accounts/checking-1"})
        self.assertNotEqual(
            changes[0]["changed_records"]["agent/accounts/checking-1"]["before_sha256"],
            changes[0]["changed_records"]["agent/accounts/checking-1"]["after_sha256"],
        )

    def test_shared_bank_account_changes_make_captured_native_evaluation_inconclusive(self):
        from copy import deepcopy
        from tau2.data_model.message import AssistantMessage, ToolCall, UserMessage

        provider_id, task_id = "tau2-banking", "task_051"
        task = self.service._load_task(provider_id, task_id)

        def evaluate_with_other_case_change(conversation_id: str, *, same_account: bool,
                                            zero_expected_changes: bool = False):
            isolated = Tau2ExternalProvider(NativeProviderStore(
                Path(self.temp.name) / (conversation_id + ".sqlite")
            ))
            prepared = isolated.prepare_case(ProviderCasePrepareRequest(
                provider_id=provider_id, task_id=task_id, conversation_id=conversation_id,
                company_actor_id="employee:fixture", company_account_reference="company-account:fixture",
                company_issue_reference="company-issue:" + conversation_id,
            ))
            self.assertEqual(prepared.initialization_status, "applied")
            baseline = isolated.store.conversation_baseline(provider_id, conversation_id)
            environment = isolated._environment(provider_id, baseline, task)
            initial = task.initial_state
            trajectory = [message.model_dump(mode="json") for message in (initial.message_history or [])]
            environment.set_state(
                initialization_data=initial.initialization_data if initial else None,
                initialization_actions=initial.initialization_actions if initial else None,
                message_history=initial.message_history or [],
            )
            for index, action in enumerate(task.evaluation_criteria.actions or []):
                call = ToolCall(id=f"synthetic-gold-{index}", name=action.name,
                                arguments=action.arguments, requestor=action.requestor)
                message = (AssistantMessage(role="assistant", tool_calls=[call])
                           if action.requestor == "assistant"
                           else UserMessage(role="user", tool_calls=[call]))
                result = environment.get_response(call)
                environment.sync_tools()
                trajectory.extend([message.model_dump(mode="json"), result.model_dump(mode="json")])
            state = isolated.store.state(provider_id)
            before_own_actions = {"agent_db": deepcopy(state["agent_db"]), "user_db": deepcopy(state["user_db"])}
            state["agent_db"], state["user_db"] = isolated._snapshot(environment, provider_id)
            case = isolated.store.conversation(provider_id, conversation_id)
            case["trajectory"] = trajectory
            own_action_op = "synthetic-original-actions:" + conversation_id
            case["company_context"]["operation_ids"].append(own_action_op)
            claim, claim_hash = isolated.store.claim(provider_id, own_action_op, {"case": conversation_id})
            self.assertEqual(claim, "claimed")
            isolated.store.complete_external_action(
                provider_id=provider_id, operation_id=own_action_op, request_hash=claim_hash,
                state=state, conversation_id=conversation_id, task_id=task_id, conversation=case,
                response={"status": "applied"}, before_state=before_own_actions,
            )

            current = isolated.store.state(provider_id)
            before_other = {"agent_db": deepcopy(current["agent_db"]), "user_db": deepcopy(current["user_db"])}
            card_table = current["agent_db"]["credit_card_accounts"]["data"]
            target_card = "cc_5e4c1a83b0_bronze"
            other_card = next(key for key, row in card_table.items() if row.get("user_id") != "5e4c1a83b0")
            changed_card = target_card if same_account else other_card
            row = card_table[changed_card]
            value = Decimal(row["current_balance"].replace("$", "").replace(",", ""))
            row["current_balance"] = f"${value + Decimal('1.00'):,.2f}"
            other_op = "synthetic-other-case-change:" + conversation_id
            other_case = {
                "agent_state": {}, "trajectory": [], "initialization_status": "applied",
                "initialization_evidence": {}, "company_context": {"operation_ids": [other_op]},
            }
            other_claim, other_hash = isolated.store.claim(provider_id, other_op, {"case": changed_card})
            self.assertEqual(other_claim, "claimed")
            isolated.store.complete_external_action(
                provider_id=provider_id, operation_id=other_op, request_hash=other_hash,
                state=current, conversation_id="other:" + conversation_id, task_id=task_id,
                conversation=other_case, response={"status": "applied"}, before_state=before_other,
            )
            capture = isolated.capture_evaluation_snapshot(provider_id, conversation_id)
            check = ProviderEvaluationSnapshotCheck(
                capture_ref=capture.capture_ref, capture_sha256=capture.capture_sha256,
            )

            class PassingReward:
                reward = 0.0 if zero_expected_changes else 1.0

                @staticmethod
                def model_dump(mode="json"):
                    return {"reward": 1.0}

            if zero_expected_changes:
                from tau2.data_model.tasks import RewardType
                non_db_task = task.model_copy(update={
                    "evaluation_criteria": task.evaluation_criteria.model_copy(
                        update={"reward_basis": [RewardType.ACTION]},
                    ),
                })
                baseline_digests = native_record_digests(baseline["agent_db"], baseline["user_db"])
                with patch.object(isolated, "_load_task", return_value=non_db_task), \
                     patch.object(isolated, "_replayed_native_record_digests", return_value=baseline_digests), \
                     patch("tau2.evaluator.evaluator.evaluate_simulation", return_value=PassingReward()) as evaluator:
                    result = isolated.evaluate_captured(check)
            else:
                with patch("tau2.evaluator.evaluator.evaluate_simulation", return_value=PassingReward()) as evaluator:
                    result = isolated.evaluate_captured(check)
            if same_account:
                evaluator.assert_not_called()
            else:
                evaluator.assert_called_once()
            return result

        same_account_result = evaluate_with_other_case_change("synthetic-share-same", same_account=True)
        self.assertEqual(same_account_result.status, "inconclusive")
        self.assertEqual(same_account_result.reason_code, "shared_native_record_changed")
        unrelated_result = evaluate_with_other_case_change("synthetic-share-unrelated", same_account=False)
        self.assertEqual(unrelated_result.status, "completed")
        zero_write_result = evaluate_with_other_case_change(
            "synthetic-share-zero-write", same_account=True, zero_expected_changes=True,
        )
        self.assertEqual(zero_write_result.status, "inconclusive")
        self.assertEqual(zero_write_result.reason_code, "shared_native_record_changed")

    def test_worker_preparation_and_evaluation_capture_routes_require_native_auth(self):
        from fastapi.testclient import TestClient
        from pydantic import SecretStr
        from servers.simulation.tau2_actor_worker import Tau2CustomerSettings, create_app

        token = "provider-worker-test-token-" + "x" * 24
        with tempfile.TemporaryDirectory() as temp:
            app = create_app(
                Tau2CustomerSettings(bearer_token=SecretStr(token)),
                Path(temp) / "actor.sqlite",
            )
            client = TestClient(app)
            request = {
                "provider_id": "tau2-airline", "task_id": "0",
                "conversation_id": "synthetic-worker-preparation",
                "company_actor_id": "employee:fixture",
                "company_account_reference": "company-account:fixture",
                "company_issue_reference": "company-issue:fixture",
            }
            self.assertEqual(client.post("/v1/external-provider/prepare", json=request).status_code, 401)
            headers = {"Authorization": "Bearer " + token}
            prepared = client.post("/v1/external-provider/prepare", json=request, headers=headers)
            self.assertEqual(prepared.status_code, 200, prepared.text)
            self.assertEqual(prepared.json()["status"], "prepared")
            captured = client.get(
                "/v1/external-provider/conversations/synthetic-worker-preparation/evaluation-snapshot",
                params={"provider_id": "tau2-airline"}, headers=headers,
            )
            self.assertEqual(captured.status_code, 200, captured.text)
            receipt = captured.json()
            self.assertNotIn("trajectory", receipt)
            self.assertNotIn("evaluation_criteria", receipt)
            from tau2.data_model.tasks import RewardType
            task = app.state.native_provider._load_task("tau2-airline", "0")
            judged_task = task.model_copy(update={
                "evaluation_criteria": task.evaluation_criteria.model_copy(update={
                    "reward_basis": [RewardType.COMMUNICATE],
                    "communicate_info": ["the customer was told the request was received"],
                }),
            })
            with patch.object(app.state.native_provider, "_load_task", return_value=judged_task):
                evaluation = client.post("/v1/external-provider/evaluate-captured", json={
                    "capture_ref": receipt["capture_ref"],
                    "capture_sha256": receipt["capture_sha256"],
                }, headers=headers)
            self.assertEqual(evaluation.status_code, 503)
            self.assertIn("configured Codex provider", evaluation.json()["detail"])
            verify = client.post("/v1/external-provider/verify-evaluation-snapshot", json={
                "capture_ref": receipt["capture_ref"], "capture_sha256": receipt["capture_sha256"],
            }, headers=headers)
            self.assertEqual(verify.status_code, 200)
            self.assertTrue(verify.json()["valid"])

    def test_banking_source_patch_merges_natively_and_conflict_detection_is_leaf_scoped(self):
        task = self.service._load_task("tau2-banking", "task_040")
        source_patch = task.initial_state.initialization_data.model_dump(mode="json", exclude_none=True)
        baseline, _ = self.service._base_databases("tau2-banking")
        self.assertEqual(_banking_patch_conflicts(baseline, baseline, source_patch), [])
        state = {"agent_db": baseline, "user_db": None}
        environment = self.service._environment("tau2-banking", state, task)
        environment.set_state(task.initial_state.initialization_data,
                              task.initial_state.initialization_actions,
                              task.initial_state.message_history or [])
        table_name = next(name for name, value in source_patch["agent_data"].items()
                          if name != "task_config" and value.get("data"))
        target_id = next(iter(source_patch["agent_data"][table_name]["data"]))
        self.assertIn(target_id, environment.tools.db.model_dump(mode="python")[table_name]["data"])
        conflict_state = json.loads(json.dumps(baseline))
        conflict_state[table_name]["data"][target_id] = {"different": "prior-company-update"}
        self.assertEqual(_banking_patch_conflicts(conflict_state, baseline, source_patch), [table_name])

    def test_banking_task_config_is_environment_local(self):
        task = self.service._load_task("tau2-banking", "task_026")
        baseline, _ = self.service._base_databases("tau2-banking")
        original_config = json.loads(json.dumps(baseline["task_config"]))
        scoped = self.service._environment("tau2-banking", {"agent_db": baseline, "user_db": None}, task)
        scoped.set_state(task.initial_state.initialization_data, None, [])
        separate = self.service._environment("tau2-banking", {"agent_db": baseline, "user_db": None}, task)
        self.assertEqual(baseline["task_config"], original_config)
        self.assertNotEqual(scoped.tools.db.task_config.model_dump(mode="python"),
                            separate.tools.db.task_config.model_dump(mode="python"))

    def test_verified_company_portal_action_uses_native_user_tool_and_is_idempotent(self):
        from tau2.agent.llm_agent import LLMAgentState

        provider_id = "tau2-banking"
        state = self.service._bootstrap(provider_id)
        task_id, conversation_id = "task_001", "synthetic-bank-portal-case"
        operation_id = "synthetic-bank-seed"
        claim, request_hash = self.service.store.claim(provider_id, operation_id, {"seed": conversation_id})
        self.assertEqual(claim, "claimed")
        context = {
            "company_id": "company", "company_actor_id": "employee:synthetic",
            "company_account_reference": "company-account:synthetic",
            "company_issue_reference": "company-issue:synthetic", "message_ids": [],
        }
        case = {
            "agent_state": LLMAgentState(system_messages=[], messages=[]).model_dump(mode="json"),
            "trajectory": [], "initialization_status": "applied", "initialization_evidence": {},
            "company_context": context,
        }
        baseline = {"agent_db": json.loads(json.dumps(state["agent_db"])), "user_db": None}
        self.service.store.commit_operation(
            provider_id=provider_id, operation_id=operation_id, request_hash=request_hash,
            state=state, conversation_id=conversation_id, task_id=task_id, conversation=case,
            baseline=None, conversation_baseline=baseline, response={"seeded": True},
        )
        action = CompanyProviderPortalAction(
            provider_id=provider_id, operation_id="synthetic-bank-action",
            conversation_id=conversation_id, company_actor_id=context["company_actor_id"],
            company_account_reference=context["company_account_reference"],
            company_issue_reference=context["company_issue_reference"], mapped_provider_user_id=None,
            native_user_tool="apply_for_credit_card",
            arguments={"card_type": "Gold Rewards Card", "customer_name": "Synthetic Portal User",
                       "annual_income": 50000, "rho_bank_subscription": False},
            portal_event="portal_form_submitted",
            evidence_references=[ProviderEvidenceReference(
                source="company_bank_portal", reference="portal-event:synthetic-1",
            )], correlation_id="synthetic-correlation-1",
        )
        with patch("tau2.environment.environment.logger.debug"):
            receipt = self.service.apply_company_portal_action(action)
        self.assertEqual(receipt.native_tool, "apply_for_credit_card")
        self.assertNotIn("Synthetic Portal User", receipt.model_dump_json())
        saved = self.service.store.conversation(provider_id, conversation_id)
        self.assertNotIn("mapped_provider_user_id", saved["company_context"])
        user_action = next(row for row in saved["trajectory"] if row.get("role") == "user")
        self.assertEqual(user_action["tool_calls"][0]["name"], "apply_for_credit_card")
        with patch("tau2.environment.environment.logger.debug"):
            replay = self.service.apply_company_portal_action(action)
        self.assertEqual(replay, receipt)

    def test_posted_financial_proof_applies_only_the_matching_native_bank_tool(self):
        from tau2.agent.llm_agent import LLMAgentState
        from tau2.data_model.message import AssistantMessage, ToolCall, ToolMessage

        provider_id, conversation_id = "tau2-banking", "synthetic-bank-transfer-case"
        state = self.service._bootstrap(provider_id)
        environment = self.service._environment(provider_id, state, self.service._load_task(provider_id, "task_001"))
        database = environment.tools.db
        selected = None
        for user_id in database.users.data:
            checking = next((
                (account_id, account) for account_id, account in database.accounts.data.items()
                if account.get("user_id") == user_id and account.get("class") == "checking"
                and account.get("status") in {"OPEN", "ACTIVE"}
                and Decimal(str(account.get("current_holdings", "0").replace("$", "").replace(",", ""))) >= 1
            ), None)
            card = next((
                (account_id, account) for account_id, account in database.credit_card_accounts.data.items()
                if account.get("user_id") == user_id
                and Decimal(str(account.get("current_balance", "$0").replace("$", "").replace(",", ""))) > 0
            ), None)
            if checking and card:
                selected = user_id, checking, card
                break
        self.assertIsNotNone(selected)
        user_id, (checking_id, checking), (card_id, card) = selected
        native_args = {"user_id": user_id, "checking_account_id": checking_id,
                       "credit_card_account_id": card_id, "amount": 1.0}
        unlock_call = ToolCall(id="synthetic-unlock", name="unlock_discoverable_agent_tool",
                               arguments={"agent_tool_name": "pay_credit_card_from_checking_9182"},
                               requestor="assistant")
        with patch("tau2.environment.environment.logger.debug"):
            unlock_result = environment.get_response(unlock_call)
        request_call = ToolCall(
            id="synthetic-requested-payment", name="call_discoverable_agent_tool",
            arguments={"agent_tool_name": "pay_credit_card_from_checking_9182",
                       "arguments": json.dumps(native_args, sort_keys=True)},
            requestor="assistant",
        )
        rejected_result = ToolMessage(
            id=request_call.id, role="tool", requestor="assistant",
            content="Call rejected pending posted company execution evidence.",
            error=True, execution_status="rejected",
        )
        native_trajectory = [
            AssistantMessage(role="assistant", tool_calls=[unlock_call]), unlock_result,
            AssistantMessage(role="assistant", tool_calls=[request_call]), rejected_result,
        ]
        from servers.simulation.tau2_external_provider import _private_json
        case = {
            "agent_state": LLMAgentState(system_messages=[], messages=native_trajectory).model_dump(mode="json"),
            "trajectory": [_private_json(row) for row in native_trajectory],
            "initialization_status": "applied", "initialization_evidence": {},
            "company_context": {
                "company_id": "company", "company_actor_id": "employee:synthetic",
                "company_account_reference": "company-account:synthetic",
                "company_issue_reference": "company-issue:synthetic", "mapped_provider_user_id": user_id,
            },
        }
        operation_id = "synthetic-finance-seed"
        _, request_hash = self.service.store.claim(provider_id, operation_id, {"seed": conversation_id})
        self.service.store.commit_operation(
            provider_id=provider_id, operation_id=operation_id, request_hash=request_hash,
            state=state, conversation_id=conversation_id, task_id="task_001", conversation=case,
            baseline=None, conversation_baseline={"agent_db": json.loads(json.dumps(state["agent_db"])), "user_db": None},
            response={"seeded": True},
        )
        before_checking = Decimal(str(checking["current_holdings"]))
        before_card = Decimal(card["current_balance"].replace("$", "").replace(",", ""))
        evidence_ref = ProviderEvidenceReference(
            source="company_erp", reference="synthetic-posted-payment",
            doctype="Payment Entry", name="SYNTHETIC-PE-1",
        )
        action = CompanyProviderFinancialAction(
            provider_id=provider_id, operation_id="synthetic-finance-apply",
            conversation_id=conversation_id, company_actor_id="employee:synthetic",
            company_account_reference="company-account:synthetic",
            company_issue_reference="company-issue:synthetic", mapped_provider_user_id=user_id,
            native_agent_tool="pay_credit_card_from_checking_9182", arguments=native_args,
            financial_execution=ProviderFinancialExecutionEvidence(
                source_type="erp_payment_entry", source_reference=evidence_ref, docstatus=1,
                status="posted", amount=Decimal("1.00"), currency="USD",
                account_reference="company-account:synthetic", occurred_at="2026-10-30T12:00:00Z",
            ), evidence_references=[evidence_ref], correlation_id="synthetic-finance-correlation",
        )
        with patch("tau2.environment.environment.logger.debug"):
            receipt = self.service.apply_company_financial_action(action)
        self.assertEqual(receipt.status, "applied")
        self.assertNotEqual(receipt.native_state_revision, state["state_revision"])
        after = self.service.store.state(provider_id)["agent_db"]
        after_checking = Decimal(after["accounts"]["data"][checking_id]["current_holdings"])
        after_card = Decimal(after["credit_card_accounts"]["data"][card_id]["current_balance"].replace("$", "").replace(",", ""))
        self.assertEqual(after_checking, before_checking - Decimal("1"))
        self.assertEqual(after_card, before_card - Decimal("1"))
        saved = self.service.store.conversation(provider_id, conversation_id)
        replacement = next(row for row in saved["trajectory"] if row.get("role") == "tool"
                           and row.get("id") == request_call.id)
        self.assertFalse(replacement["error"])
        self.assertEqual(self.service.apply_company_financial_action(action), receipt)

    @staticmethod
    def request(task_id: str, *, operation_id: str, conversation_id: str, message_id: str) -> ProviderRequest:
        return ProviderRequest(
            provider_id="tau2-telecom", operation_id=operation_id,
            conversation_id=conversation_id, task_id=task_id,
            company_actor_id="employee:fixture-1", company_account_reference="company-account:fixture-1",
            company_issue_reference="company-issue:fixture-1", message_id=message_id,
            in_reply_to=None, subject="Overdue mobile service", body="Please review the affected company line.",
        )

    def test_source_manifest_and_native_domain_loaders_use_the_pinned_data(self):
        manifest = self.service._verify_assets()
        self.assertEqual(manifest["upstream_commit"], TAU2_SOURCE_COMMIT)
        self.assertEqual(manifest["providers"][0]["task_count"], 50)
        self.assertEqual(manifest["providers"][1]["provider_id"], "tau2-telecom")
        self.assertEqual(manifest["providers"][1]["task_count"], 2285)
        telecom_tasks_path = RUNTIME / "data/tau2/domains/telecom/tasks.json"
        telecom_bytes = telecom_tasks_path.read_bytes()
        telecom_rows = json.loads(telecom_bytes)
        telecom_asset = next(asset for asset in manifest["assets"]
                             if asset["path"] == "data/tau2/domains/telecom/tasks.json")
        self.assertEqual(hashlib.sha256(telecom_bytes).hexdigest(), telecom_asset["sha256"])
        self.assertEqual(len(telecom_rows), 2285)
        self.assertEqual(len({task["id"] for task in telecom_rows}), 2285)
        self.assertEqual(telecom_asset["sha256"],
                         "37e562e1ae3242577407e1303b1548bc64e7ea68e37d36173e6747990ceaf8a4")
        self.assertEqual(sum(any(action["name"] in {"refuel_data", "send_payment_request", "make_payment"}
                                 for action in task["evaluation_criteria"]["actions"])
                             for task in telecom_rows), 1128)
        self.assertEqual(sum(any(action["name"] == "refuel_data" for action in task["evaluation_criteria"]["actions"])
                             for task in telecom_rows), 1120)
        self.assertEqual(sum(any(action["name"] == "make_payment" for action in task["evaluation_criteria"]["actions"])
                             for task in telecom_rows), 8)
        self.assertEqual(sum(any(action["name"] == "send_payment_request" for action in task["evaluation_criteria"]["actions"])
                             for task in telecom_rows), 8)
        self.assertTrue(all(task["initial_state"] is not None for task in telecom_rows))
        from tau2.domains.telecom import environment as telecom_environment
        split_file = RUNTIME / "data/tau2/domains/telecom/split_tasks.json"
        with patch.object(telecom_environment, "TELECOM_TASK_SET_PATH", split_file.parent / "tasks.json"):
            source_splits = telecom_environment.get_tasks_split()
        self.assertEqual(len(source_splits["base"]), 114)
        self.assertEqual(len(set(source_splits["base"])), 114)
        self.assertEqual(len(set(source_splits["full"])), 2285)
        self.assertEqual(len(set(source_splits["base"]) & {row["id"] for row in telecom_rows}), 114)
        self.assertEqual(PROVIDER_DOMAIN_FILES["tau2-telecom"], ("telecom", "tasks.json"))
        self.assertEqual(PROVIDER_DOMAIN_FILES["tau2-airline"], ("airline", "tasks.json"))
        task = self.service._load_task("tau2-airline", "0")
        original = next(item for item in json.loads(
            (RUNTIME / "data/tau2/domains/airline/tasks.json").read_text()
        ) if item["id"] == "0")
        self.assertEqual(task.id, original["id"])
        self.assertEqual(
            [(action.action_id, action.name, action.requestor) for action in task.evaluation_criteria.actions],
            [(action["action_id"], action["name"], action["requestor"])
             for action in original["evaluation_criteria"]["actions"]],
        )
        state = self.service._bootstrap("tau2-airline")
        environment = self.service._environment("tau2-airline", state)
        self.assertIn("cancel_reservation", {tool.name for tool in environment.get_tools()})
        self.assertEqual(state["upstream_commit"], TAU2_SOURCE_COMMIT)

    def test_native_refuel_write_returns_observed_draft_bill_evidence(self):
        from tau2.data_model.message import ToolCall

        agent_db, user_db = self.service._base_databases("tau2-telecom")
        state = {"agent_db": agent_db, "user_db": user_db}
        environment = self.service._environment("tau2-telecom", state)
        customer = next(customer for customer in environment.tools.db.customers if customer.line_ids)
        line_id = customer.line_ids[0]
        arguments = {"customer_id": customer.customer_id, "line_id": line_id, "gb_amount": 1.0}
        before = {bill.bill_id: bill.model_dump(mode="json") for bill in environment.tools.db.bills}
        result = environment.get_response(ToolCall(
            id="synthetic-refuel-1", name="refuel_data", arguments=arguments, requestor="assistant",
        ))
        self.assertFalse(result.error)
        evidence = self.service._refuel_bill_evidence(
            environment, arguments, before,
            self.request("synthetic-refuel-task", operation_id="synthetic-refuel-op",
                         conversation_id="synthetic-refuel-thread", message_id="<synthetic-refuel@example.test>"),
            "synthetic-refuel-task",
        )
        self.assertEqual(evidence.event, "refuel_charge_added")
        self.assertEqual(evidence.provider_bill_status, "Draft")
        self.assertEqual(evidence.native_references[0].source_id, customer.customer_id)
        self.assertEqual(evidence.native_references[1].source_id, line_id)
        bill = environment.tools._get_bill_by_id(evidence.native_references[2].source_id)
        self.assertEqual(bill.customer_id, customer.customer_id)
        self.assertTrue(any(item.description == evidence.charge_description and
                            Decimal(str(item.amount)) == evidence.charge_amount
                            for item in bill.line_items))
        self.assertEqual(Decimal(str(bill.total_due)), evidence.bill_total_due)
        self.assertGreater(evidence.charge_amount, 0)
        self.assertEqual(evidence.currency, "USD")
        before_second = {item.bill_id: item.model_dump(mode="json") for item in environment.tools.db.bills}
        result = environment.get_response(ToolCall(
            id="synthetic-refuel-2", name="refuel_data", arguments=arguments, requestor="assistant",
        ))
        self.assertFalse(result.error)
        second_evidence = self.service._refuel_bill_evidence(
            environment, arguments, before_second,
            self.request("synthetic-refuel-task", operation_id="synthetic-refuel-op-2",
                         conversation_id="synthetic-refuel-thread", message_id="<synthetic-refuel-2@example.test>"),
            "synthetic-refuel-task",
        )
        self.assertEqual(second_evidence.native_references[2].source_id, evidence.native_references[2].source_id)
        self.assertNotEqual(second_evidence.evidence_id, evidence.evidence_id)
        self.assertEqual(second_evidence.charge_line_index, evidence.charge_line_index + 1)

    def test_persisted_telecom_payment_requires_erp_post_and_calls_native_user_tool(self):
        provider_id = "tau2-telecom"
        task_id = "[service_issue]overdue_bill_suspension[PERSONA:Easy]"
        task = self.service._load_task(provider_id, task_id)
        source_request_action = next(
            action for action in task.evaluation_criteria.actions
            if action.requestor == "assistant" and action.name == "send_payment_request"
        )
        upstream_reply = [
            AssistantMessage(role="assistant", tool_calls=[ToolCall(
                id="native-payment-request", name=source_request_action.name,
                arguments=source_request_action.arguments, requestor="assistant",
            )]),
            AssistantMessage(role="assistant", content="I have sent the bill request.", tool_calls=None),
        ]
        request = self.request(task_id, operation_id="provider-op-1",
                               conversation_id="company-issue-thread-1", message_id="<company-message-1@example.test>")
        with patch("tau2.agent.llm_agent.generate", side_effect=upstream_reply):
            response = self.service.handle(request)
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.initialization_status, "applied")
        self.assertEqual(response.company_actor_id, request.company_actor_id)
        self.assertEqual(response.company_account_reference, request.company_account_reference)
        self.assertEqual(response.in_reply_to, request.message_id)
        self.assertNotIn("evaluation_criteria", response.model_dump_json())

        state = self.service.store.state(provider_id)
        provider_env = self.service._environment(provider_id, state)
        payment_request = provider_env.user_tools.db.surroundings.payment_request
        self.assertIsNotNone(payment_request)
        requested_bill = next(bill for bill in provider_env.tools.db.bills
                              if bill.bill_id == payment_request.bill_id)
        self.assertEqual(requested_bill.status, BillStatus.AWAITING_PAYMENT)

        payment = CompanyPaymentEvidence(
            company_actor_id=request.company_actor_id,
            company_account_reference=request.company_account_reference,
            company_issue_reference=request.company_issue_reference,
            erp_doctype="Payment Entry", erp_name="SYNTHETIC-PE-1", docstatus=1,
            provider_bill_id=payment_request.bill_id,
            amount=Decimal(str(payment_request.amount_due)), currency="USD",
            document_modified="2026-10-30 12:00:00",
            native_references=[{"doctype": "Purchase Invoice", "name": "SYNTHETIC-PI-1"}],
        )
        payment_receipt = self.service.apply_company_payment(
            operation_id="provider-payment-1", conversation_id=request.conversation_id,
            evidence=payment,
        )
        self.assertEqual(payment_receipt.native_tool, "make_payment")
        self.assertEqual(payment_receipt.erp_name, "SYNTHETIC-PE-1")
        self.assertEqual(payment_receipt.provider_bill_status, "Paid")
        self.assertTrue(any(reference.get("domain") == "telecom" and
                            reference.get("source_id") == payment.provider_bill_id
                            for reference in payment_receipt.native_references))
        state_after = self.service.store.state(provider_id)
        after = self.service._environment(provider_id, state_after)
        bill_after = next(bill for bill in after.tools.db.bills
                          if bill.bill_id == payment.provider_bill_id)
        self.assertEqual(bill_after.status, BillStatus.PAID)
        self.assertIsNone(after.user_tools.db.surroundings.payment_request)

        private_case = self.service.store.conversation(provider_id, request.conversation_id)
        user_payment = next(message for message in private_case["trajectory"]
                            if message.get("role") == "user" and message.get("tool_calls"))
        self.assertEqual(user_payment["tool_calls"][0]["name"], "make_payment")
        self.assertEqual(private_case["company_context"]["message_ids"], [request.message_id])
        replay = self.service.apply_company_payment(
            operation_id="provider-payment-1", conversation_id=request.conversation_id,
            evidence=payment,
        )
        self.assertEqual(replay, payment_receipt)
        reopened = Tau2ExternalProvider(NativeProviderStore(self.database))
        self.assertEqual(reopened.store.state(provider_id)["state_revision"], state_after["state_revision"])

    def test_conflicting_source_initialization_keeps_the_shared_provider_state_and_is_inconclusive(self):
        first = "[service_issue]overdue_bill_suspension[PERSONA:Easy]"
        second = "[service_issue]airplane_mode_on|overdue_bill_suspension[PERSONA:None]"
        source_reply = [
            AssistantMessage(role="assistant", content="The company line is already under review.", tool_calls=None),
            AssistantMessage(role="assistant", content="The shared account remains in its current state.", tool_calls=None),
        ]
        first_request = self.request(first, operation_id="provider-init-1",
                                     conversation_id="company-issue-thread-2", message_id="<company-message-2@example.test>")
        with patch("tau2.agent.llm_agent.generate", side_effect=source_reply[:1]):
            first_result = self.service.handle(first_request)
        before = self.service.store.state("tau2-telecom")
        second_request = self.request(second, operation_id="provider-init-2",
                                      conversation_id="company-issue-thread-3", message_id="<company-message-3@example.test>")
        with patch("tau2.agent.llm_agent.generate", side_effect=source_reply[1:]):
            second_result = self.service.handle(second_request)
        after = self.service.store.state("tau2-telecom")
        self.assertEqual(first_result.initialization_status, "applied")
        self.assertEqual(second_result.initialization_status, "conflict")
        self.assertEqual(before["state_revision"], after["state_revision"])
        case = self.service.store.conversation("tau2-telecom", second_request.conversation_id)
        self.assertEqual(case["initialization_evidence"]["provider_state_revision_before"],
                         case["initialization_evidence"]["provider_state_revision_after"])
        result = self.service.evaluate_conversation("tau2-telecom", second_request.conversation_id)
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.reason_code, "shared_provider_initialization_conflict")


if __name__ == "__main__":
    unittest.main()

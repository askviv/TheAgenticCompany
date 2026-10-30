"""SQLite lifecycle and durability contracts for the native provider store."""

from __future__ import annotations

from copy import deepcopy
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from servers.simulation.provider_store import NativeProviderStore, native_record_digests


class NativeRecordDigestTests(unittest.TestCase):
    def test_list_rows_use_their_own_identity_before_parent_customer_id(self):
        agent_db = {
            "bills": [
                {"customer_id": "customer-1", "bill_id": "bill-1", "total_due": 10.0},
                {"customer_id": "customer-1", "bill_id": "bill-2", "total_due": 20.0},
            ],
            "lines": [
                {"customer_id": "customer-1", "line_id": "line-1", "status": "active"},
                {"customer_id": "customer-1", "line_id": "line-2", "status": "suspended"},
            ],
        }
        digests = native_record_digests(agent_db, None)
        self.assertEqual(set(digests), {
            "agent/bills/bill-1", "agent/bills/bill-2",
            "agent/lines/line-1", "agent/lines/line-2",
        })
        changed = deepcopy(agent_db)
        changed["bills"][0]["total_due"] = 11.0
        after = native_record_digests(changed, None)
        self.assertEqual(
            {ref for ref in digests if digests[ref] != after[ref]},
            {"agent/bills/bill-1"},
        )

    def test_duplicate_native_primary_identity_fails_instead_of_overwriting_a_digest(self):
        with self.assertRaisesRegex(ValueError, "duplicate record identities"):
            native_record_digests({"bills": [
                {"customer_id": "customer-1", "bill_id": "bill-1", "total_due": 10.0},
                {"customer_id": "customer-2", "bill_id": "bill-1", "total_due": 20.0},
            ]}, None)


class _TrackingConnection(sqlite3.Connection):
    def close(self) -> None:
        self.was_closed = True
        super().close()


class NativeProviderStoreConnectionTests(unittest.TestCase):
    def test_handles_close_and_native_state_baseline_and_idempotency_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "provider.sqlite"
            native_connect = sqlite3.connect
            connections: list[_TrackingConnection] = []

            def tracking_connect(*args, **kwargs):
                connection = native_connect(*args, **kwargs, factory=_TrackingConnection)
                connection.was_closed = False
                connections.append(connection)
                return connection

            with patch("servers.simulation.provider_store.sqlite3.connect", side_effect=tracking_connect):
                store = NativeProviderStore(path)
                initial = {"users": {"data": {"user-1": {"status": "active"}}}}
                state = store.bootstrap("tau2-banking", "pinned-source", initial, None)
                request = {"operation": "synthetic-request"}
                status, request_hash = store.claim("tau2-banking", "operation-1", request)
                self.assertEqual(status, "claimed")
                state["agent_db"]["users"]["data"]["user-1"]["status"] = "reviewed"
                case = {
                    "agent_state": {"messages": [], "system_messages": []},
                    "trajectory": [], "initialization_status": "applied",
                    "initialization_evidence": {"status": "applied"},
                    "company_context": {"company_id": "company", "source_ids": ["user-1"]},
                }
                store.commit_operation(
                    provider_id="tau2-banking", operation_id="operation-1",
                    request_hash=request_hash, state=state,
                    conversation_id="conversation-1", task_id="task-001",
                    conversation=case, baseline=None,
                    conversation_baseline={"agent_db": deepcopy(initial), "user_db": None},
                    response={"status": "completed"},
                )

                restarted = NativeProviderStore(path)
                persisted = restarted.state("tau2-banking")
                self.assertEqual(persisted["agent_db"]["users"]["data"]["user-1"]["status"], "reviewed")
                self.assertEqual(
                    restarted.conversation("tau2-banking", "conversation-1"),
                    {**case, "task_id": "task-001"},
                )
                self.assertEqual(
                    restarted.conversation_baseline("tau2-banking", "conversation-1"),
                    {"agent_db": initial, "user_db": None},
                )
                replay_status, replay = restarted.claim("tau2-banking", "operation-1", request)
                self.assertEqual(replay_status, "completed")
                self.assertEqual(replay, {"status": "completed"})
                conflict_status, _ = restarted.claim(
                    "tau2-banking", "operation-1", {"operation": "different-request"},
                )
                self.assertEqual(conflict_status, "conflict")

            self.assertTrue(connections)
            self.assertTrue(all(connection.was_closed for connection in connections))


if __name__ == "__main__":
    unittest.main()

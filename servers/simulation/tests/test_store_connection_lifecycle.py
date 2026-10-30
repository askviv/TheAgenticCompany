"""SQLite connection lifecycle for the native background and actor stores."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from servers.simulation.actor_worker_store import ActorTurnStore
from servers.simulation.coffeebench_background import _ModelResponseStore


class _TrackingConnection(sqlite3.Connection):
    def close(self) -> None:
        self.was_closed = True
        super().close()


class StoreConnectionLifecycleTests(unittest.TestCase):
    def test_background_and_actor_store_operations_close_sqlite_handles(self):
        with tempfile.TemporaryDirectory() as directory:
            connections: list[_TrackingConnection] = []
            native_connect = sqlite3.connect

            def tracking_connect(*args, **kwargs):
                connection = native_connect(*args, **kwargs, factory=_TrackingConnection)
                connection.was_closed = False
                connections.append(connection)
                return connection

            with patch("sqlite3.connect", side_effect=tracking_connect):
                model_store = _ModelResponseStore(Path(directory) / "coffee-model.sqlite")
                self.assertEqual(model_store.load_or_claim("turn-1", "hash-1"), (None, "turn-1"))
                model_store.complete("turn-1", "hash-1", {"content": "cached"})
                self.assertEqual(model_store.load_or_claim("turn-1", "hash-1"), ({"content": "cached"}, "turn-1"))

                actor_store = ActorTurnStore(Path(directory) / "actor-turns.sqlite")
                self.assertEqual(actor_store.claim("turn-2", "hash-2"), ("claimed", None))
                actor_store.complete("turn-2", "hash-2", {"status": "completed"})
                self.assertEqual(
                    actor_store.claim("turn-2", "hash-2"),
                    ("completed", {"status": "completed"}),
                )

            self.assertTrue(connections)
            self.assertTrue(all(connection.was_closed for connection in connections))


if __name__ == "__main__":
    unittest.main()

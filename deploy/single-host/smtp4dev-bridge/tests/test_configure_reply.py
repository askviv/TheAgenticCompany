from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "configure_reply.py"
SPEC = importlib.util.spec_from_file_location("configure_smtp4dev_reply", SCRIPT)
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class Response:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


class ConfigureReplyTests(unittest.TestCase):
    def credentials_file(self, directory: Path) -> Path:
        path = directory / "service-credentials.json"
        path.write_text(json.dumps({"accounts": {
            "viv_reply_relay": {
                "allow_spoofing": False,
                "enable_imap": False,
                "mail": {
                    "address": "viv_reply_relay@example.com",
                    "password": "test-password-sentinel",
                    "status": "ready",
                },
            },
        }}))
        path.chmod(0o600)
        return path

    def test_configures_only_native_authenticated_relay_and_preserves_recipient_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.credentials_file(Path(directory))
            settings = {
                "relaySmtpServer": "old.host",
                "relaySmtpPort": 25,
                "relayTlsMode": "None",
                "relayLogin": "",
                "relayPassword": "",
                "relaySenderAddress": "",
                "relayAutomaticEmails": [],
                "relayAutomaticRelayExpression": "recipient.toLowerCase().endsWith('@example.com') ? recipient : null",
                "unrelatedSetting": "preserved",
            }
            requests = []
            readbacks = []

            def opener(request, timeout):
                self.assertEqual(timeout, 15)
                requests.append(request)
                if request.get_method() == "GET":
                    # The native API may redact its stored secret on readback.
                    readable = {**settings, "relayPassword": ""}
                    readbacks.append(readable)
                    return Response(200, json.dumps(readable).encode())
                self.assertEqual(request.get_method(), "POST")
                settings.update(json.loads(request.data))
                return Response(200, b"")

            status = configure.configure_reply_relay(
                "http://127.0.0.1:19157", path, opener=opener,
            )

            self.assertEqual(status, 200)
            self.assertEqual(settings["relaySmtpServer"], "mail.company.example.test")
            self.assertEqual(settings["relaySmtpPort"], 2587)
            self.assertEqual(settings["relayTlsMode"], "StartTls")
            self.assertEqual(settings["relayLogin"], "viv_reply_relay@example.com")
            self.assertEqual(settings["relayPassword"], "test-password-sentinel")
            self.assertEqual(settings["relayAutomaticEmails"], [])
            self.assertEqual(settings["relayAutomaticRelayExpression"],
                             "recipient.toLowerCase().endsWith('@example.com') ? recipient : null")
            self.assertEqual(settings["unrelatedSetting"], "preserved")
            self.assertEqual([request.get_method() for request in requests], ["GET", "POST", "GET"])
            self.assertEqual(requests[0].full_url, "http://127.0.0.1:19157/api/Server")
            self.assertEqual(readbacks[-1]["relayPassword"], "")

    def test_retries_stale_readback_without_reposting(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.credentials_file(Path(directory))
            original = {
                "relaySmtpServer": "old.host",
                "relaySmtpPort": 25,
                "relayTlsMode": "None",
                "relayLogin": "",
                "relayPassword": "",
                "relaySenderAddress": "",
                "relayAutomaticEmails": [],
                "relayAutomaticRelayExpression": "recipient.endsWith('@example.com') ? recipient : null",
            }
            desired = None
            get_count = 0
            post_count = 0

            def opener(request, timeout):
                nonlocal desired, get_count, post_count
                if request.get_method() == "GET":
                    get_count += 1
                    current = desired if get_count >= 3 else original
                    return Response(200, json.dumps(current).encode())
                post_count += 1
                desired = json.loads(request.data)
                return Response(200, b"")

            configure.configure_reply_relay("http://127.0.0.1:19157", path, opener=opener)
            self.assertEqual(post_count, 1)
            self.assertEqual(get_count, 3)
            self.assertEqual(desired["relaySmtpPort"], 2587)

    def test_persisted_secret_mismatch_fails_safely_after_bounded_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.credentials_file(Path(directory))
            settings = {
                "relaySmtpServer": "mail.company.example.test",
                "relaySmtpPort": 2587,
                "relayTlsMode": "StartTls",
                "relayLogin": "viv_reply_relay@example.com",
                "relayPassword": "different-secret",
                "relaySenderAddress": "",
                "relayAutomaticEmails": [],
                "relayAutomaticRelayExpression": "recipient.endsWith('@example.com') ? recipient : null",
            }
            gets = 0

            def opener(request, timeout):
                nonlocal gets
                if request.get_method() == "GET":
                    gets += 1
                    return Response(200, json.dumps(settings).encode())
                return Response(200, b"")

            with self.assertRaisesRegex(RuntimeError, "relayPassword") as raised:
                configure.configure_reply_relay("http://127.0.0.1:19157", path, opener=opener)
            self.assertEqual(gets, 6)  # initial read plus five bounded persistence checks
            self.assertNotIn("different-secret", str(raised.exception))
            self.assertNotIn("test-password-sentinel", str(raised.exception))

    def test_insecure_credentials_file_is_rejected_before_api_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.credentials_file(Path(directory))
            path.chmod(0o644)
            with self.assertRaisesRegex(PermissionError, "mode 0600"):
                configure.load_relay_credentials(path)

    def test_postfix_sender_alias_is_exact_and_layered_before_mailu_default(self):
        root = SCRIPT.parents[3]
        postfix = (root / "deploy/single-host/mailu/overrides/postfix/postfix.cf").read_text()
        sender_map = (root / "deploy/single-host/mailu/overrides/postfix/viv-sender-login.map").read_text()
        self.assertIn(
            "smtpd_sender_login_maps = lmdb:/etc/postfix/viv-sender-login.map, \\${podop}senderlogin",
            postfix,
        )
        entries = [line.split() for line in sender_map.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(entries, [["agent@agent.example.test", "viv_reply_relay@example.com"]])
        self.assertNotIn("*", sender_map)


if __name__ == "__main__":
    unittest.main()

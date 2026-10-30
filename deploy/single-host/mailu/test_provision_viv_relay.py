from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).with_name("provision.py")
SPEC = importlib.util.spec_from_file_location("mailu_provision", SCRIPT)
provision = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provision)


class RelayAccountTests(unittest.TestCase):
    def test_dedicated_account_is_nonspoofing_smtp_only_and_pending_credentials_are_durable(self):
        class Client:
            def get_user(self, _email):
                return None

            def user_exists(self, _email):
                return False

        accounts = {}
        outcome = provision.ensure_viv_relay_account(Client(), accounts)
        record = accounts["viv_reply_relay"]
        self.assertEqual(outcome, "pending")
        self.assertEqual(record["mail"]["address"], "viv_reply_relay@example.com")
        self.assertTrue(record["mail"]["password"])
        self.assertEqual(record["mail"]["status"], "pending")
        self.assertIs(record["allow_spoofing"], False)
        self.assertIs(record["enable_imap"], False)

    def test_existing_spoof_enabled_account_is_refused_without_modification(self):
        class Client:
            def get_user(self, _email):
                return {"allow_spoofing": True, "global_admin": False, "enabled": True, "enable_imap": False}

            def user_exists(self, _email):
                return True

        original = {
            "viv_reply_relay": {
                "mail": {"address": "viv_reply_relay@example.com", "password": "test-password-sentinel", "status": "ready"},
                "allow_spoofing": False, "enable_imap": False,
            }
        }
        with self.assertRaisesRegex(RuntimeError, "non-spoofing"):
            provision.ensure_viv_relay_account(Client(), original)
        self.assertEqual(original["viv_reply_relay"]["mail"]["password"], "test-password-sentinel")
        self.assertEqual(original["viv_reply_relay"]["mail"]["status"], "pending")

    def test_uncertain_create_with_mismatched_saved_password_stays_pending(self):
        class Client:
            def get_user(self, _email):
                return {"allow_spoofing": False, "global_admin": False,
                        "enabled": True, "enable_imap": False}

        accounts = {"viv_reply_relay": {
            "allow_spoofing": False,
            "enable_imap": False,
            "mail": {"address": "viv_reply_relay@example.com",
                     "password": "saved-from-uncertain-create", "status": "pending"},
        }}
        with patch.object(provision, "authenticate_viv_relay", side_effect=provision.smtplib.SMTPAuthenticationError(535, b"invalid")) as auth:
            with self.assertRaisesRegex(RuntimeError, "did not authenticate"):
                provision.ensure_viv_relay_account(Client(), accounts)
        auth.assert_called_once_with("viv_reply_relay@example.com", "saved-from-uncertain-create")
        self.assertEqual(accounts["viv_reply_relay"]["mail"]["password"], "saved-from-uncertain-create")
        self.assertEqual(accounts["viv_reply_relay"]["mail"]["status"], "pending")

    def test_uncertain_create_is_recovered_only_after_saved_password_authenticates(self):
        class Client:
            def get_user(self, _email):
                return {"allow_spoofing": False, "global_admin": False,
                        "enabled": True, "enable_imap": False}

        accounts = {"viv_reply_relay": {
            "allow_spoofing": False,
            "enable_imap": False,
            "mail": {"address": "viv_reply_relay@example.com",
                     "password": "saved-from-uncertain-create", "status": "pending"},
        }}
        with patch.object(provision, "authenticate_viv_relay") as auth:
            self.assertEqual(provision.ensure_viv_relay_account(Client(), accounts), "preserved")
        auth.assert_called_once_with("viv_reply_relay@example.com", "saved-from-uncertain-create")
        self.assertEqual(accounts["viv_reply_relay"]["mail"]["status"], "ready")

    def test_native_credential_check_uses_starttls_auth_without_sending(self):
        instances = []

        class SMTP:
            def __init__(self, host, port, timeout):
                self.args = (host, port, timeout)
                self.calls = []
                instances.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def ehlo(self):
                self.calls.append(("ehlo",))

            def starttls(self, *, context):
                self.calls.append(("starttls", context))

            def login(self, username, password):
                self.calls.append(("login", username, password))

        with patch.object(provision.smtplib, "SMTP", SMTP):
            provision.authenticate_viv_relay("relay@example.com", "secret")
        self.assertEqual(instances[0].args, ("mail.company.example.test", 2587, 15))
        self.assertEqual([call[0] for call in instances[0].calls], ["ehlo", "starttls", "ehlo", "login"])
        self.assertEqual(instances[0].calls[-1], ("login", "relay@example.com", "secret"))

    def test_account_creation_payload_disables_spoofing_imap_and_admin(self):
        class Client(provision.MailuClient):
            def __init__(self):
                self.payload = None

            def request(self, method, path, payload=None):
                self.payload = payload
                return None

        client = Client()
        created = client.create_user(
            provision.VIV_RELAY_EMAIL,
            "Viv Reply Relay",
            "private-test-password",
            enable_imap=False,
        )
        self.assertTrue(created)
        self.assertEqual(client.payload["email"], "viv_reply_relay@example.com")
        self.assertIs(client.payload["allow_spoofing"], False)
        self.assertIs(client.payload["enable_imap"], False)
        self.assertIs(client.payload["global_admin"], False)


if __name__ == "__main__":
    unittest.main()

import unittest
import requests

from servers.experiments.runner import RocketChatClient, RocketChatError


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response({"status": "success", "data": {"authToken": "auth", "userId": "operator"}})

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return Response(next(self.payloads))


class RocketChatClientTests(unittest.TestCase):
    def test_dispatch_creates_channel_invites_configured_user_and_posts_mention(self):
        session = Session([
            {"success": True, "user": {"_id": "viv-id"}},
            {"success": True, "channels": []},
            {"success": True, "channel": {"_id": "room-id"}},
            {"success": True, "members": []},
            {"success": True},
            {"success": False, "errorType": "error-invalid-message"},
            {"success": True, "message": {"_id": "exp-run-7"}},
        ])
        client = RocketChatClient("http://chat.example", "operator", "secret", "viv", session=session)

        result = client.dispatch("run-7", "Check the report", "Review the latest report and reply here.")

        self.assertEqual(result, {"room_id": "room-id", "message_id": "exp-run-7"})
        post = session.calls[-1][2]["json"]["message"]
        self.assertEqual(post["rid"], "room-id")
        self.assertEqual(post["_id"], "exp-run-7")
        self.assertIn("@viv", post["msg"])
        self.assertIn("Experiment run-7: Check the report", post["msg"])
        self.assertIn("Please reply in this thread", post["msg"])
        self.assertEqual(session.calls[-3][2]["json"], {"roomId": "room-id", "userId": "viv-id"})
        self.assertEqual(session.calls[1][2]["headers"]["X-Auth-Token"], "auth")

    def test_thread_returns_root_and_replies_with_normalized_authors(self):
        session = Session([
            {"success": True, "user": {"_id": "viv-id"}},
            {"success": True, "message": {"_id": "root-id", "msg": "Please review", "u": {"username": "operator"}, "ts": {"$date": 1000}}},
            {"success": True, "messages": [{"_id": "reply-id", "msg": "Done", "u": {"username": "viv"}, "ts": {"$date": 2000}}]},
        ])
        client = RocketChatClient("http://chat.example", "operator", "secret", "viv", session=session)

        messages = client.thread("room-id", "root-id")

        self.assertEqual([message["message_id"] for message in messages], ["root-id", "reply-id"])
        self.assertEqual(messages[1]["author"], "viv")
        self.assertEqual(messages[1]["text"], "Done")
        self.assertEqual(messages[0]["timestamp"], "1970-01-01T00:00:01+00:00")

    def test_uncertain_send_recovers_preexisting_idempotency_message(self):
        class UncertainSession(Session):
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                if url.endswith("chat.getMessage"):
                    return Response({"success": False, "errorType": "error-invalid-message"})
                if url.endswith("channels.list"):
                    return Response({"success": True, "channels": [{"_id": "room-id", "name": "viv-experiments"}]})
                if url.endswith("channels.members"):
                    return Response({"success": True, "members": [{"_id": "viv-id"}]})
                if url.endswith("chat.sendMessage"):
                    raise requests.ConnectionError("connection lost after post")
                return Response({"success": True, "user": {"_id": "viv-id"}})

        session = UncertainSession([])
        original_request = session.request
        message_already_accepted = False

        def request_with_recovery(method, url, **kwargs):
            nonlocal message_already_accepted
            if url.endswith("chat.sendMessage"):
                session.calls.append((method, url, kwargs))
                message_already_accepted = True
                raise requests.ConnectionError("connection lost after post")
            if url.endswith("chat.getMessage") and message_already_accepted:
                session.calls.append((method, url, kwargs))
                return Response({"success": True, "message": {"_id": "exp-run-8"}})
            return original_request(method, url, **kwargs)

        session.request = request_with_recovery
        client = RocketChatClient("http://chat.example", "operator", "secret", "viv", session=session)

        result = client.dispatch("run-8", "Review", "Review this.")

        self.assertEqual(result, {"room_id": "room-id", "message_id": "exp-run-8"})
        self.assertEqual(sum(call[1].endswith("chat.sendMessage") for call in session.calls), 1)

    def test_transport_errors_propagate(self):
        class BrokenSession(Session):
            def request(self, method, url, **kwargs):
                raise OSError("network unavailable")

        client = RocketChatClient("http://chat.example", "operator", "secret", "viv", session=BrokenSession([]))
        client._token = "auth"
        client._user_id = "operator"
        with self.assertRaisesRegex(OSError, "network unavailable"):
            client._request("GET", "users.info")

    def test_api_error_on_http_404_is_normalized_with_http_cause(self):
        class NotFoundSession(Session):
            def request(self, method, url, **kwargs):
                return Response({"success": False, "errorType": "error-invalid-message"}, 404)

        client = RocketChatClient("http://chat.example", "operator", "secret", "viv", session=NotFoundSession([]))
        client._token = "auth"
        client._user_id = "operator"

        with self.assertRaises(RocketChatError) as raised:
            client._request("GET", "chat.getMessage")

        self.assertEqual(raised.exception.code, "error-invalid-message")
        self.assertIsInstance(raised.exception.__cause__, requests.HTTPError)
        self.assertEqual(raised.exception.__cause__.response.status_code, 404)

    def test_rocket_chat_53_missing_message_response_is_treated_as_absent(self):
        class MissingMessageSession(Session):
            def request(self, method, url, **kwargs):
                return Response({"success": False}, 400)

        client = RocketChatClient(
            "http://chat.example", "operator", "secret", "viv", session=MissingMessageSession([])
        )
        client._token = "auth"
        client._user_id = "operator"

        self.assertIsNone(client._get_message("exp-run-9"))

    def test_get_message_does_not_treat_permission_or_provider_errors_as_missing(self):
        class ErrorSession(Session):
            def __init__(self, response):
                super().__init__([])
                self.response = response

            def request(self, method, url, **kwargs):
                return self.response

        client = RocketChatClient("http://chat.example", "operator", "secret", "viv")
        client._token = "auth"
        client._user_id = "operator"
        cases = (
            Response({"success": False}, 403),
            Response({"success": False, "error": "message lookup failed"}, 400),
        )
        for response in cases:
            with self.subTest(status=response.status_code, body=response.payload):
                client.session = ErrorSession(response)
                with self.assertRaises(RocketChatError):
                    client._get_message("exp-run-9")

    def test_webhook_setup_uses_scoped_configuration_without_returning_secret(self):
        session = Session([
            {"success": True, "user": {"_id": "viv-id"}},
            {"success": True, "channels": [{"_id": "room-id", "name": "viv-experiments"}]},
            {"success": True, "members": [{"_id": "viv-id"}]},
            {"success": True, "integrations": []},
            {"success": True, "integration": {"_id": "integration-id"}},
        ])
        client = RocketChatClient("http://chat.example", "operator", "secret", "viv", session=session)

        result = client.setup_outgoing_webhook("private-token")

        self.assertEqual(result, "integration-id")
        payload = session.calls[-1][2]["json"]
        self.assertEqual(payload["name"], "TACExperimentTracking")
        self.assertEqual(payload["type"], "webhook-outgoing")
        self.assertEqual(payload["channel"], "#viv-experiments")
        self.assertEqual(payload["event"], "sendMessage")
        self.assertEqual(payload["urls"], ["http://192.0.2.10:8110/api/chat-events"])
        self.assertFalse(payload["scriptEnabled"])
        self.assertEqual(payload["token"], "private-token")
        self.assertNotIn("private-token", result)


if __name__ == "__main__":
    unittest.main()

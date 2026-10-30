import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_coworker import (
    Account, BrowserCoworker, BrowserSession, BrowserTurnError, FillCredential,
    Navigate, Observe, PointerClick, TypeText,
)
from playwright.async_api import async_playwright, expect


class CompanySite(BaseHTTPRequestHandler):
    submissions = []
    provider_requests = []

    def log_message(self, *args):
        pass  # Test HTTP requests deliberately omit fixture credentials from output.

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "session=sarah" not in self.headers.get("Cookie", ""):
            page = '''<form method="post" action="/login">
              <label>Username<input name="username"></label>
              <label>Password<input name="password" type="password"></label>
              <button>Log in</button></form>'''
        else:
            page = '''<form method="post" action="/submit">
              <label>Full name<input name="name"></label>
              <label>Decision<select name="decision"><option value="no">Reject</option>
                <option value="yes">Approve</option></select></label>
              <label><input type="checkbox" name="confirm">Confirm</label>
              <label>Notes<textarea name="notes"></textarea></label>
              <button>Submit request</button></form>'''
        self.wfile.write(page.encode())

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        if self.path == "/v1/chat/completions":
            request = json.loads(body)
            self.provider_requests.append(request)
            if any(m["role"] == "tool" for m in request["messages"]):
                message = {"role": "assistant", "content": "Ready."}
                finish = "stop"
            else:
                message = {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "observe", "type": "function",
                    "function": {"name": "Observe", "arguments": "{}"},
                }]}
                finish = "tool_calls"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": "fixture", "object": "chat.completion", "created": 1,
                "model": "fixture", "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode())
            return
        form = parse_qs(body)
        if self.path == "/login":
            if form != {"username": ["sarah"], "password": ["fixture-password"]}:
                self.send_error(403)
                return
            self.send_response(303)
            self.send_header("Set-Cookie", "session=sarah; Path=/; Max-Age=3600; HttpOnly")
            self.send_header("Location", "/form")
            self.end_headers()
        elif self.path == "/submit" and "session=sarah" in self.headers.get("Cookie", ""):
            self.submissions.append(form)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<h1>Request submitted</h1>")
        else:
            self.send_error(403)


def answer(text):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=text, tool_calls=[]))])


def tool(name, **arguments):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="tool_calls", message=SimpleNamespace(content=None, tool_calls=[
            SimpleNamespace(id="call", function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))
        ]))])


def observation(messages):
    return json.loads(next(message["content"] for message in reversed(messages)
                           if message["role"] == "tool" and "snapshot" in message["content"]))


def ref(messages, label):
    snapshot = observation(messages)["snapshot"]
    match = re.search(r'"' + re.escape(label) + r'"[^\n]*\[ref=([^\]]+)\]', snapshot)
    if not match:
        raise AssertionError(f"Missing fixture element {label}: {snapshot}")
    return match[1]


class BrowserCoworkerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CompanySite)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LITELLM_MODEL": "fixture-tool-model", "NPC_COMPANY_ID": "company-a"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.accounts = Path(self.temp.name) / "accounts.json"
        self.accounts.write_text(json.dumps({"Sarah Johnson": {"viv": {
            "login_url": self.url + "/login", "username": "sarah", "password": "fixture-password",
        }}}))

    def worker(self, complete, name="Sarah Johnson", **kwargs):
        return BrowserCoworker(name, state_dir=self.temp.name,
                               accounts_file=str(self.accounts), complete=complete, **kwargs)

    async def test_login_fill_submit_and_keep_authenticated_session_isolated(self):
        """Actual HTTP submission must contain the intended coworker values exactly once."""
        CompanySite.submissions.clear()
        actions = iter([
            lambda m: tool("Navigate", url=self.url + "/login"),
            lambda m: tool("Observe"),
            lambda m: tool("FillCredential", ref=ref(m, "Username"), account="viv", field="username"),
            lambda m: tool("FillCredential", ref=ref(m, "Password"), account="viv", field="password"),
            lambda m: tool("Click", ref=ref(m, "Log in")),
            lambda m: tool("Observe"),
            lambda m: tool("Fill", ref=ref(m, "Full name"), value="Sarah Johnson"),
            lambda m: tool("Select", ref=ref(m, "Decision"), value="yes"),
            lambda m: tool("Check", ref=ref(m, "Confirm"), checked=True),
            lambda m: tool("Click", ref=ref(m, "Notes")),
            lambda m: tool("TypeText", text="Scenario approval"),
            lambda m: tool("Click", ref=ref(m, "Submit request")),
            lambda m: tool("Observe"),
        ])
        pictures = []

        async def complete(**request):
            self.assertNotIn("fixture-password", json.dumps(request))
            pictures.extend(message for message in request["messages"] if isinstance(message["content"], list))
            action = next(actions, None)
            if action:
                return action(request["messages"])
            self.assertIn("Request submitted", observation(request["messages"])["snapshot"])
            return answer("I've submitted the request.")

        worker = self.worker(complete)
        self.assertEqual(await worker.respond("Approve the request using your scenario information."),
                         "I've submitted the request.")
        self.assertEqual(CompanySite.submissions, [{"name": ["Sarah Johnson"], "decision": ["yes"],
                                                   "confirm": ["on"], "notes": ["Scenario approval"]}])
        self.assertTrue(pictures, "The model must receive browser screenshots, not just tool acknowledgements")
        audit = (worker.directory / "actions.jsonl").read_text()
        for private_value in ("fixture-password", "Scenario approval", self.url):
            self.assertNotIn(private_value, audit)

        async def inspect_session(**request):
            tools = [m for m in request["messages"] if m["role"] == "tool"]
            if not tools:
                return tool("Navigate", url=self.url + "/form")
            if len(tools) == 1:
                return tool("Observe")
            return answer(observation(request["messages"])["snapshot"])

        self.assertIn("Full name", await self.worker(inspect_session).respond("Inspect your session."))
        self.assertIn("Log in", await self.worker(inspect_session, name="Bob Smith").respond("Inspect your session."))
        with patch.dict(os.environ, {"NPC_COMPANY_ID": "company-b"}):
            self.assertIn("Log in", await self.worker(inspect_session).respond("Inspect your session."))

    async def test_visual_click_and_typing_really_change_the_page(self):
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.set_content('<label>Name<input></label><button onclick="this.textContent=\'Clicked\'">Go</button>')
                session = BrowserSession(context, {})
                box = await page.get_by_label("Name").bounding_box()
                await session.execute(PointerClick(x=box["x"] + box["width"] / 2, y=box["y"] + box["height"] / 2))
                await session.execute(TypeText(text="Coworker"))
                await expect(page.get_by_label("Name")).to_have_value("Coworker")
                box = await page.get_by_role("button").bounding_box()
                await session.execute(PointerClick(x=box["x"] + 2, y=box["y"] + 2))
                await expect(page.get_by_role("button")).to_have_text("Clicked")
            finally:
                await browser.close()

    async def test_credentials_cannot_be_filled_on_another_origin(self):
        async with async_playwright() as driver:
            browser = await driver.chromium.launch()
            try:
                context = await browser.new_context()
                page = await context.new_page()
                await page.goto(self.url)
                session = BrowserSession(context, {"viv": Account(
                    login_url="https://other-company.invalid/login", username="sarah", password="fixture-password")})
                view, _ = await session.execute(Observe())
                password_ref = re.search(r'"Password"[^\n]*\[ref=([^\]]+)\]', view["snapshot"])[1]
                result, _ = await session.execute(FillCredential(ref=password_ref, account="viv", field="password"))
                self.assertEqual(result, {"error": "credential_origin_mismatch"})
                await expect(page.get_by_label("Password")).to_have_value("")
            finally:
                await browser.close()

    async def test_provider_failure_preserves_cause_and_reports_failure(self):
        failure = RuntimeError("private provider detail")

        async def complete(**kwargs):
            raise failure

        with self.assertRaises(BrowserTurnError) as raised:
            await self.worker(complete).respond("Open the form.")
        self.assertEqual(raised.exception.code, "execution_failed")
        self.assertIs(raised.exception.__cause__, failure)
        self.assertNotIn("private provider detail", str(raised.exception))

    async def test_actual_litellm_transport_sends_tools_and_screenshot_results(self):
        CompanySite.provider_requests.clear()
        with patch.dict(os.environ, {"LITELLM_MODEL": "openai/fixture", "LITELLM_API_KEY": "local-fixture",
                                     "LITELLM_BASE_URL": self.url + "/v1"}):
            self.assertEqual(await self.worker(None).respond("Inspect the browser."), "Ready.")
        first, second = CompanySite.provider_requests
        self.assertTrue(any(t["function"]["name"] == "FillCredential" for t in first["tools"]))
        self.assertTrue(any(m["role"] == "tool" and "snapshot" in m["content"] for m in second["messages"]))
        self.assertTrue(any(isinstance(m["content"], list) and m["content"][0]["type"] == "image_url"
                            for m in second["messages"]))

    async def test_step_exhaustion_is_a_failure_not_a_completed_action(self):
        async def complete(**kwargs):
            return tool("Observe")

        with self.assertRaises(BrowserTurnError) as raised:
            await self.worker(complete, max_steps=1).respond("Open the form.")
        self.assertEqual(raised.exception.code, "action_limit_exceeded")


if __name__ == "__main__":
    unittest.main()

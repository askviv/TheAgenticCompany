import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser_coworker import BrowserTurnError
from human_user_agent import HumanUserAgent
from sotopia.messages import Observation


class HumanBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_coworker_turn_uses_tools_and_returns_reply_to_existing_chat_bridge(self):
        agent = HumanUserAgent(agent_name="Sarah Johnson", model_name="fixture")
        with patch.dict(os.environ, {"NPC_BROWSER_ENABLED": "1"}), \
                patch("browser_coworker.BrowserCoworker.respond", new_callable=AsyncMock,
                      return_value="I submitted the form.") as respond:
            action = await agent.aact(Observation(last_turn="Viv: Please complete this form.",
                                                 turn_number=1, available_actions=["speak", "leave"]))
        self.assertEqual(action.action_type, "speak")
        self.assertEqual(action.argument, "I submitted the form.")
        self.assertIn("Please complete this form.", respond.call_args.args[0])

    async def test_browser_failure_is_an_explicit_failed_reply_not_silent_none(self):
        agent = HumanUserAgent(agent_name="Sarah Johnson", model_name="fixture")
        error = BrowserTurnError("execution_failed", "fixture-reference")
        with patch.dict(os.environ, {"NPC_BROWSER_ENABLED": "1"}), \
                patch("browser_coworker.BrowserCoworker.respond", new_callable=AsyncMock, side_effect=error), \
                self.assertLogs(level="ERROR") as logs:
            action = await agent.aact(Observation(last_turn="Viv: Complete this form.",
                                                 turn_number=1, available_actions=["speak", "leave"]))
        self.assertEqual(action.action_type, "speak")
        self.assertIn("couldn't complete", action.argument)
        self.assertIn("fixture-reference", action.argument)
        self.assertEqual(len(logs.output), 1)


if __name__ == "__main__":
    unittest.main()

# Coworker browser actions

The optional browser coworker extends the existing Sotopia `HumanUserAgent`.
Rocket.Chat messages still arrive through `RocketChatAgent`; browser actions run
before the coworker's final text reply is sent back through that same bridge.
The scenario, persona, judging criteria and evaluator remain unchanged.

## Runtime

Build the base workspace image with `--build-arg INSTALL_NPC_BROWSER=1`, or install
`browser-requirements.txt` and run `python -m playwright install --with-deps chromium`
in the coworker runtime. This uses Playwright's browser engine and native tools,
including screenshots and coordinate-based mouse/keyboard interaction. It does
not automate the host desktop or require access to the operator's browser.

Enable with `NPC_BROWSER_ENABLED=1`. Use the existing `LITELLM_MODEL`,
`LITELLM_API_KEY` and `LITELLM_BASE_URL` configuration. The selected model/provider
must support tool calling and image input; this path does not use the Codex judge
or imply that its model/account authentication works for coworkers.

Other settings:

- `NPC_BROWSER_STATE_DIR`: private mounted directory, default `/npc/browser-state`.
- `NPC_COMPANY_ID`: sandbox identity, defaulting to `BOT_URL`. Use distinct values
  for companies that share a chat URL.
- `NPC_BROWSER_MAX_STEPS`: bounded model/tool rounds per reply, default 40.
- `NPC_BROWSER_ACCOUNTS_FILE`: optional read-only private JSON file, described below.
- `AGENT_USERNAME`: actual Viv Rocket.Chat username. The existing chat bridge
  accepts messages only from this sender.

The current upstream listener handles direct messages, not channel mentions or
experiment threads. This extension does not replace that listener. Thread routing
must be integrated separately before claiming that channel-based experiments work.

## Account integration contract

Identity provisioning remains owned by the company/account integration. Mount a
mode-0600 JSON credential file outside the repository and evaluator/examinee files:

```json
{
  "Sarah Johnson": {
    "viv": {
      "login_url": "https://your-viv-host.example/login",
      "username": "sarah@example.invalid",
      "password": "replace-at-runtime"
    }
  }
}
```

Keys must match the Sotopia coworker's full name. Multiple named accounts are
supported. The model sees account names and login URLs; `FillCredential` fills
username/password directly, and only on the configured login origin. Add a
separate account entry for an actual identity-provider origin if required.
Missing credentials fail the affected tool call explicitly; public forms and
other browser work remain available.

Browser profiles persist per company and coworker in mode-0700 directories,
preserving sessions between turns and process restarts. Do not mount these into
the examinee or reuse them across companies. One active process owns each profile.
For Viv accounts, run NPCs in a dedicated coworker container with the credential
file and state volume mounted there. Running browser NPCs alongside an examinee
with the same OS user does not isolate their credentials.

The base image's unpublished LiteLLM `1.23.16` pin is corrected to `1.61.20`,
matching the repository's existing Poetry lock. The selected LLM and benchmark
rubrics are not changed.

## Behavior and evidence

Tools cover page observations, navigation, clicks, text filling, select controls,
checkboxes, keyboard shortcuts, visual coordinate clicks, scrolling and tab
switching. Popups belong to the same isolated browser context. Each turn creates
and closes its browser cleanly while keeping the persistent profile.

The model must observe the page to obtain element refs and inspect the result
before claiming success. Password fields are masked in screenshots and known
account passwords are redacted from textual tool results and replies. Page
content and screenshots are sent to the configured coworker model; use sandbox
data. Browser cookies/profiles are sensitive.

`actions.jsonl` records correlation ID, step, tool name and outcome without
arguments, URLs, screenshots, form contents or credentials. Unexpected failures
preserve their cause and produce an explicit failed coworker reply. Timeouts are
reported to the model so it can inspect the page before deciding whether to retry;
an action may have happened before a timeout. There is no automatic resubmission.

Local regression checks (real Chromium, deterministic model responses):

```sh
python -m unittest discover -s workspaces/base_image/npc/tests -v
```

These verify browser execution, not live model decisions, Viv integration or
deployment. Existing benchmark scoring is not extended to browser actions by this
change; new interaction scenarios need their own observable success criteria.

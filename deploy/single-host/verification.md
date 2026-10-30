# Integration verification

Verification is separate from experiment execution and agent performance.
Use synthetic evidence and controlled transports for integration checks.

Check source-to-company record mapping, native permissions, email delivery and
reply correlation, read-state handling, timeout scheduling and duplicate
prevention. Confirm that drafts, requests and approvals cannot masquerade as
executed financial transactions. Test saved evaluations across database reopen,
idempotent retries, run generations and missing or malformed evidence.

Native service checks should use read-only operations or clearly labelled,
transactionally rolled-back fixtures. Keep captured operational reports, host
inventories, account identifiers and private evidence outside the repository.
Do not run live benchmark episodes, model turns, experiments or the agent under
test as an infrastructure smoke test.

Project checks live in servers/simulation/tests, servers/company/tests,
servers/experiments/tests and the native adapter test directories. Use the
appropriate isolated dependency environment. A passing integration check is
not a benchmark score or proof that an agent completed a business process.

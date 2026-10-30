# Automated security scanning

The `Security scanning` workflow checks the repository on pull requests to
`main`, pushes to `main`, and every Monday. It runs OSV-Scanner for known
dependency vulnerabilities and Gitleaks against the complete Git history for
passwords, API keys, and other secret-like values. Dependabot checks the configured dependency manifests weekly.

The scanning jobs read repository contents; OSV also writes code-scanning results. Pull requests use the
`pull_request` event and do not receive repository secrets; the workflow does
not check out a fork with privileged credentials or execute its scripts. The
Gitleaks scan uses the official CLI image pinned by digest, runs with
`--redact`, and does not upload its report. Its logs contain finding type,
occurrence count, and affected paths only. The image, report, and redirected
scanner output stay on the ephemeral runner.

Review findings before changing dependencies or recording an exception. This
repository contains synthetic credentials in benchmark fixtures that may
trigger secret detection. The checked-in exceptions cover three confirmed non-credential test inputs or
benchmark identifiers, each restricted to its exact file and value. Do not
ignore whole fixture trees, vendored directories or Git history. If a finding could be a real credential, rotate
it through its owner and remove it from the repository history as appropriate.

OSV-Scanner findings appear in the Actions run and GitHub code scanning when
available. Its vulnerability database requires network access to OSV. Gitleaks
performs local pattern matching and does not test whether a detected value is
live or contact the corresponding service.

Action versions are pinned to full commit SHAs. Update those pins deliberately
after checking the upstream release and reviewing the changed workflow
behavior.

## Dependency maintenance

Company service requirements are compiled from their adjacent `requirements.in`
files using uv, targeting the Linux/Python 3.12 deployment. Regenerate the pinned
transitive requirements after changing direct dependencies. Vendored native
benchmark locks are updated without changing their tasks or scoring criteria.
The tau actor uses uv overrides for LiteLLM and filelock; CoffeeBench uses
aligned OpenTelemetry API, SDK and semantic-convention overrides. These lift
upstream dependency caps to patched releases while preserving the source pins.
Controlled import/adapter tests cover these paths, not live model behaviour.

The unused legacy OpenHands baseline runner and its automated workflows have
been removed. The original task-image evaluator workflow is manual-only; normal
security checks do not launch benchmark episodes. Dependabot groups weekly
updates by ecosystem and manifest to reduce redundant pull requests. Superseded
security runs are cancelled automatically.

The troubleshooting task's `requirements.txt.fixture` is inert grading input,
not an installation manifest. Its original bytes are copied to the same
`/utils/requirements.txt` location in the task image. Its task, starting environment
and evaluator criteria are unchanged.

## Unresolved upstream constraints

SignalR Core 1.0.2 requires exactly MessagePack 1.1.2. Security advisories require
MessagePack 1.2.3 or later, but that cannot satisfy the current package metadata.
The service keeps a normally installable dependency set and the scanner finding
remains visible. Do not add an ignore or install with `--no-deps` to conceal it.

The legacy Sotopia coworker runtime requires OpenAI 1.x; patched LiteLLM releases
require OpenAI 2.x. Its old LiteLLM pin remains an unresolved dependency issue in
the optional workplace image, separate from the Codex-backed company actors.
A compatible upstream update or a separately reviewed coworker migration is
required. No model calls or live benchmark episodes validate these changes.

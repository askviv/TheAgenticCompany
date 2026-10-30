# τ-bench worker provenance

The separate Python 3.12 worker environment is locked by `pyproject.toml` and
`uv.lock`. It installs the official `sierra-research/tau2-bench` repository at
commit `7aad81c37a3a78b4ed3e4866769f851400c64415` (τ-bench 2.0.0, MIT).

The worker also needs τ's published English text persona scaffold. The upstream
Python package does not install its repository `data/` directory, so
`data/tau2/multilingual/en/pack.yaml` is copied byte-for-byte from that same
commit. SHA-256:

```text
6453ef2c2f06ca34fb0a4f90841780913a2f7af28b680a08be402190876e6435
```

No τ source code is vendored. The worker invokes the upstream `Task` and
`UserSimulator` implementation; model calls are bridged through the configured
Codex CLI custom LiteLLM provider. This repository does not claim a completed
live model call.

## Retail task catalogue

The English retail benchmark inputs required by the company retail task library
are copied byte-for-byte from the same τ-bench commit into
`data/tau2/domains/retail/`: the complete `db.json`, `tasks.json`,
`policy.md`, and `split_tasks.json`. The database contains the original 50
products, 500 users, and 1,000 orders. The task file contains 114 source tasks.
`servers/simulation/tau2_retail.py` validates those files and loads tasks using
τ's own retail domain loader. Task manifests preserve the complete original
task object and original source IDs, with a task-linked subset of the original
database records. No task `initial_state` is synthesized; upstream retail
tasks all use the shared database baseline.

The authenticated isolated worker exposes `/v1/retail/catalog` with only task
IDs/count and source provenance; `/v1/retail/tasks/{task_id}/manifest` returns
the private task, reference criteria, source integrity and linked DB records;
`/v1/retail/seed-email` runs the original task scenario through the same τ
`UserSimulator`, durable turn store, and Codex transport as `/v1/turn`; and
`/v1/retail/evaluate` accepts a native `RetailDB` projection plus τ `Message`
trajectory. The evaluator calls τ's `EnvironmentEvaluator`, action and
communication evaluators, and `NLAssertionsEvaluator`. NL judging is routed to
the already-configured Codex CLI provider inside the worker; absent NL criteria
remain neutral under τ's evaluator. No customer/Viv process is given access to
the private worker endpoint or golden manifest.

The pinned source task set has eight incomplete rows, surfaced in each manifest
as `source_integrity.status = "failed"` with the missing IDs; no replacement
records are generated. Tasks 2–4 reference absent product `6086499569`; task
39 and tasks 67–68 have customer lookup instructions with no source user match;
tasks 46–47 reference absent orders `#9502126` and `#9502127`. A native golden
action replay failure is likewise reported as unevaluated DB reward, since the
upstream evaluator otherwise catches and logs such failures before comparing
the unchanged state.

SHA-256 checksums:

```text
db.json          413a65160adbdb5fde0ffc0015c49b6d70250b10c18128de169b597af7766765
tasks.json       8e03ebce7901bd6218e7a7dc3105faa9324091a68058f7fe61c65262868812e8
policy.md        2c9652afbce57d6e087768d37cda64d31c53d50b3e3225cfdb791bac66466467
split_tasks.json ed0580ec52575b63fbf76568af42490da6ee7783ecb4aa81af46961291358f20
```

The upstream MIT license is included as `TAU2-LICENSE.txt`.

## External provider adapters

`data/tau2/domains/airline/` contains the byte-for-byte pinned Airline DB,
policy, 50 source tasks and split file used by the company-side external
provider adapter. `data/tau2/domains/telecom/` contains the pinned Telecom DB,
customer/device DB, provider policies, the official split file, and 1,128 unchanged source Task rows whose reference
trajectory includes one of the native bill-affecting writes: `refuel_data`,
`send_payment_request`, or `make_payment`. `refuel_data` adds plan-priced
charges to a provider customer bill; the latter two request and record payment
of an existing bill. The file selects source rows without rewriting task data.
All selected rows have upstream `initial_state` patches, applied through the
native environment setup against shared provider state. Checksums, task IDs, native
loader/evaluator names and the shared-provider-state policy are recorded in
`data/tau2/domains/external_provider_manifest.json`.

The upstream Telecom `base` split contains 114 task IDs; 56 intersect the
1,128 bill-affecting candidate rows. `split_tasks.json` is copied unchanged.

The adapter uses τ's native `LLMAgent`, Airline/Telecom `Environment` and
`evaluate_simulation`, with the already configured Codex CLI provider as the
inference transport. It stores one provider-native database per provider ID
across cases. Task initial-state setup is run through upstream native actions;
when those actions conflict with a previously changed shared provider record,
the existing state is retained and that conversation is reported
inconclusive. The company-side response records native Telecom bill evidence
for successful `refuel_data` writes, including the actual draft status, source
line item, amount, provider date, bill totals and source IDs; it does not create
or claim a posted ERP invoice or settlement. Company user-side Telecom
`make_payment` is propagated only from a host-validated, submitted native ERP
Payment Entry matched to the outstanding source bill and linked company
account, and is reconciled against the native paid bill state. These company-adapted integrations
retain the unchanged private τ Task and evaluator data; they are not unchanged
τ benchmark executions or benchmark-agent API access.

`data/tau2/domains/banking_knowledge/` contains the complete pinned Banking
source tree (815 files: the transactional DB, 97 native task JSON files,
provider policy, and 698 knowledge documents). The adapter loads tasks through
`tau2.domains.banking_knowledge.environment.get_tasks`, constructs the official
`get_environment` with the native `bm25_grep` retrieval variant, and evaluates
with τ's unchanged `evaluate_simulation`. The Banking data contains 53 task
initialization-data patches; these are applied through upstream `Environment`
setup after checking patched source fields against the shared provider DB.
Conflicting changes retain the existing provider data and make only that
conversation inconclusive. Banking `task_config` patches are applied to the
conversation environment without persisting them to the shared provider DB.

The company adapter keeps provider user-side actions separate from provider
agent reasoning. Verified company portal actions invoke the original native
Banking user tools; actual payment or transfer actions also require matching
posted company execution evidence. A provider-side payment or transfer request
is withheld from the τ tool until a matching posted Payment Entry or bank
transaction is supplied, then the original native tool is applied and
reconciled. The native Banking user toolkit is never exposed to the provider
LLM. `rank-bm25` is locked as an explicit dependency because upstream's native
knowledge retrieval extra requires it for `bm25_grep`.

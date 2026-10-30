# CoffeeBench upstream runtime

Source repository: `SakanaAI/CoffeeBench`
Reviewed commit: `9e2f3950b5812c8753de81ede9c68babc024b5c8`
License: Apache-2.0; see [LICENSE](LICENSE).

The complete upstream `coffeebench/` Python package is preserved byte-for-byte in
`upstream/coffeebench/`. `upstream/SHA256SUMS` records the SHA-256 digest of every
vendored upstream Python source file. The upstream README, project metadata and
locked dependencies are preserved alongside it. Company-clock checkpointing and
transaction projection live in `servers/simulation/coffeebench_background.py`;
these are adapters around CoffeeBench's native `Environment`, `BusinessApp`,
`Marketplace`, and event handlers, not replacements for its economy.

The company provider treats CoffeeBench's six business books as external
counterparty evidence. It emits source intents only for actual upstream deals,
invoices, and invoice payments where one side matches a business ID explicitly
mapped by the company. ERPNext remains authoritative for company documents and
payments. Provider checkpoints are trusted private coordinator state; their
pickle payloads must never be accepted from an API caller.

# Full upstream catalogue audit

Audit date: 30 October 2026. This inventory describes upstream source, not
installed runnable library entries or verified benchmark results. No scenario,
provider conversation or Viv test was run for this audit.

| Source | Pinned revision | Catalogue |
| --- | --- | --- |
| SakanaAI/CoffeeBench | `9e2f3950b5812c8753de81ede9c68babc024b5c8` | Nine experiment TOMLs, 21 configured seed runs |
| QwenLM/E-CommerceBench | `b84678071c424fc94713b0a9e1ae86c519bd74c7` | One configurable 365-day commerce episode, not a discrete task bank |
| sierra-research/tau2-bench | `7aad81c37a3a78b4ed3e4866769f851400c64415` | 2,746 non-mock tasks plus ten mock/test tasks |

## τ-bench

Canonical domain data is under `data/tau2/domains/`. The catalogue contains:

| Domain | Unique tasks |
| --- | ---: |
| Airline | 50 |
| Retail | 114 |
| Telecom (full) | 2,285 |
| Banking knowledge | 97 |
| Intake | 200 |
| Mock (test only) | 10 |

Telecom small and policy/workflow variants must not be counted as new unique
tasks. Banking uses 97 individual `tasks/task_*.json` files; its aggregate JSON
is not an additional task set. Original Task evaluation criteria can depend on
reference actions, native database assertions, communication assertions and
natural-language assertions selected by `reward_basis`. Importing instructions
alone cannot preserve evaluation. Each domain requires its actual upstream
environment, database, tools, policy and evaluators.

## CoffeeBench

The nine TOMLs are `experiments/roaster_focal_{gemini,glm,gpt,haiku,heuristic,kimi,opus,passive,sonnet}.toml`.
They compare the focal Roaster-A agent in the same coffee economy over 90 days;
most specify three seeds, while GLM, Kimi and heuristic specify one. They are
model/agent comparisons, not nine independent company problems. The six-business
world and its bank, inventories and cash/invoice/deal lifecycle are implemented
in the full upstream runtime. The company background runs this native world and
projects transactions involving its mapped `roaster_A` into company finance
reconciliation. It does not run the TOML comparison/evaluation runner or claim
those benchmark scores.

## E-CommerceBench

The full episode uses the upstream storefront/inventory/accounting economy and
its native assets/negotiation/fraud/solvency metrics. Inputs include 6,886 products,
60 categories, 12 store types, 576 suppliers, ten calendar events and eight
promotions. These data rows are not individual benchmark assignments. The
runner's repeated `run_index` changes logging identity; its environment is
constructed without varying the default seed. Repetitions must not be described
as independent stochastic worlds without further evidence.

## Company integration boundary

The company uses one ERPNext site and one shared company clock. A later
implementation follow-up uses the full pinned CoffeeBench native economy as a
company background provider and uses actual Coffee deal/invoice/payment facts
involving the explicitly mapped `roaster_A` business for draft native finance
documents. The source's nine TOML experiment configurations are comparisons
within that same 90-day economy; they are not nine new company assignments.

E-CommerceBench contributes native date-specific supplier-demand and supply
disruption signals to the same supplier and company. It has no accounts-payable
schedule in the selected integration, so it creates no source invoice or fake
payable. The company's native ERP remains authoritative for documents and
finance. These selective company integrations do not run the full original
E-Commerce benchmark or claim its metrics.

The pinned τ retail source contains 114 original tasks. The company retail task
library keeps the upstream task/evaluator data and maps task state through the
company ERP/email boundary. Eight source rows contain missing database
references and remain explicitly incomplete; the adapter does not synthesize
replacement records. This is not a claim that all 114 tasks have been run or
passed. See [shared company background and finance operations](company-background-finance.md)
for the operator-level lifecycle and limitations.

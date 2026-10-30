# Evaluation utilities

The unused OpenHands baseline runner has been removed. Shared-company runs use
the coordinator and native benchmark evaluator adapters; their scoring criteria
are unchanged. See [company feedback](../docs/enterprise-simulation/company-feedback.md).

`generate_task_images.py` lists original workplace image references.
`summarise_results.py` reads historical baseline outputs; install its dependency
with `poetry install --with reporting`. Neither utility launches an agent.

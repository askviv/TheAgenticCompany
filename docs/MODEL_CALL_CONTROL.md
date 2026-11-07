# Stop company model calls

The dashboard header has **Stop all model calls** and **Resume model calls**.
This is independent of the CoffeeBench and E-CommerceBench switches: those
switches stop their providers, while the global control covers employee CUA,
background generation, and judging through the shared inference service.

Stopping persists `model-calls-stopped` in the inference service's existing
private `CODEX_HOME`, blocks new CLI launches, and kills active CLI process groups.
It does not stop the shared company clock or erase company data. Requests already
submitted to a model provider cannot be undone; local execution is cancelled.
Resume explicitly removes the marker. Restarting the service preserves stopped
state. Provider switches retain their own settings.

The dashboard forwards authenticated control requests to the existing inference
service. POST requests retain the dashboard's session CSRF protection. A failed
control request is displayed as an error, never as a confirmed stop.

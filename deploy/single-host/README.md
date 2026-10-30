# Single-host deployment templates

These templates configure the workplace applications on a Linux host using
Docker Compose. They are examples, not a record of a particular installation.
Set the host address, domain, service account, storage paths and private
configuration for your deployment before starting services. Reserved example
addresses must be replaced with your actual configuration.

Keep application data, credentials and operational reports outside Git. Back up
the current databases and private configuration before changing an existing
deployment. Starting and stopping services must preserve volumes and company
records; do not use volume deletion or benchmark reset commands as stop actions.

The enterprise coordinator and actor services have separate deployment guides
under deploy/enterprise-simulation. Application identities and provider bindings
are defined in company-identities.json. Review the company mail, HTTPS and
identity connector templates for the chosen deployment.

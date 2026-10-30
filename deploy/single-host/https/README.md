# HTTPS deployment configuration

Configure hostnames, DNS, certificate storage and reverse-proxy upstreams for
the target deployment. Reserved example domains are documentation values, not
hosted endpoints. Use the applications' supported trusted-host/proxy settings
and retain their existing authentication and data.

Keep private keys, DNS credentials, certificates and configuration snapshots
outside Git. Validate certificate trust and application redirects using the
configured names. HTTPS health checks prove transport, not benchmark outcomes.

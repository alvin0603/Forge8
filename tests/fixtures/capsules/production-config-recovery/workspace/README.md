# Streamgate configuration fixture

Streamgate reads a base INI file followed by an environment overlay. Options in
the overlay replace options from the base section; unspecified options retain
their base values. Unknown sections and options are rejected.

Production invariants enforced by `streamgate.config` include:

- worker count is between 1 and 32;
- shutdown grace is greater than the outbound request timeout;
- `max_inflight` cannot exceed 64 work items per worker;
- the collector URL is HTTPS and TLS verification is enabled;
- a bearer token resolves from the environment;
- the spool directory is an absolute path.

The CLI performs the same validation used during service startup:

```bash
python3 -m streamgate --base config/base.ini --overlay config/production.ini --check
```

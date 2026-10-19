# Security policy

Distributed Compute runs code you submit on machines you connect, over SSH.
Security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through GitHub:
**Security → Report a vulnerability** on this repository. Include what you
found, how to reproduce it, and its impact. We will acknowledge the report
within a few days and keep you updated until it is fixed and released.

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | Yes |

## Running it safely

- By default every port listens on `127.0.0.1` only. Before exposing it with
  `BIND_ADDR=0.0.0.0`, set `ENVIRONMENT=production`, set long random
  `JWT_SECRET` and `SSH_CRED_KEY` values, and put HTTPS in front: worker
  passwords for onboarding travel in request bodies.
- Use a dedicated, non-admin account on each worker. Tasks run with its
  permissions; the sandbox isolates files and processes, not the kernel.
- Give each person their own token, with an expiry
  (`generate_token.py --hours`).

Known limits are listed in the README.

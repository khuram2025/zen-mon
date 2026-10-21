# Access-control end-to-end suite

`test_access_control_e2e.py` drives a real API over HTTP (delegation limits, last-administrator
invariant including a concurrent race, zero-permission denial across the API, streams, lockout,
MFA, sessions, directory-mapping validation, ...). It creates and deletes users and roles, so
point it **only** at a disposable database.

1. `initdb` a scratch PostgreSQL and a scratch Redis (the sign-in rate limiter uses it).
2. `scripts/run-migrations.py --scripts-dir scripts --include-init ...` against the scratch DB.
3. `python server/tests/integration/access_control/seed.py` (same `DATABASE_URL`).
4. Start the API (`uvicorn app.main:app --port 8191`) with that `DATABASE_URL`, `REDIS_URL`,
   a `JWT_SECRET` of at least 32 characters and a 64-hex `SNMP_ENC_KEY`. The API refuses to
   start on placeholder secrets (`changeme`); a scratch stack that uses them needs
   `ZENPLUS_ALLOW_INSECURE_DEFAULTS=1`.
5. `ZENPLUS_ACCESS_E2E_URL=http://127.0.0.1:8191 pytest server/tests/integration/test_access_control_e2e.py`
   (set `ZENPLUS_ACCESS_E2E_DSN` if the scratch Postgres is not `127.0.0.1:55432`).

The run takes a few minutes: the API limits sign-ins to 30/min per IP and TOTP replay protection
forces one 31-second wait. Re-runs are safe (all names are unique) but leave extra rows behind.

# zentryc.com — Intake questions for the appliance team

> **Purpose.** The remote server is built; before the appliance side commits to a v1 cut we need to confirm a small number of contract details and pull a few artifacts out of the server team's hands. Each question below is here because its answer either changes appliance code or gates an end-to-end smoke test. Anything not on this list is unlikely to matter for v1.
>
> Please answer inline, or reply with the JSON/curl outputs requested.

---

## A. API contract — does what's deployed match what the agent expects?

The agent code at `/opt/zenplus/updater/` is the source of truth for the appliance side. The contract it relies on is in `Documentation/13-SHIP-READY-MASTER-PLAN.md §7.1`. We need confirmation, not theory, on each of the following.

1. **Base URL.** What exactly does the appliance hit?
   - [ ] `https://zentryc.com/api/v1/...` (production)
   - [ ] `https://staging.zentryc.com/api/v1/...` (staging) — give us this one for smoke tests, please.

2. **`POST /api/v1/appliances/register` — paste a sample successful response.** We need to confirm the JSON keys are exactly `appliance_id`, `api_key`, and `subscription`. The agent breaks if any of those is renamed (e.g., `applianceId`, `token`).

3. **`POST /api/v1/appliances/checkin` — paste a sample of both response shapes.**
   - "no update": expected `{"next_action": "none", "release": null, "subscription": {...}}`
   - "update available": expected `{"next_action": "update", "release": {...}, "subscription": {...}}`
   - Confirm the `release` object includes `id`, `version`, `changelog`, `severity`, `package_url`, `package_sha256`, `manifest_sig` (base64), and `size`. Anything missing or renamed?

4. **`manifest_sig` delivery.** Two valid options; tell us which one you implemented:
   - (a) Server returns base64 in the checkin response, and the `.zup` does NOT contain `manifest.json.sig`.
   - (b) Server returns empty string, and the `.zup` contains `manifest.json.sig` itself.
   - Either works; we just need to know so we can stop confusing ourselves in tests.

5. **`GET /api/v1/updates/download/{release_id}` — direct serve or 302 redirect?**
   - If 302: target host(s)? S3? R2? Same domain? We need to confirm `httpx.Client(follow_redirects=True)` is sufficient and that the redirect target accepts our auth headers — or doesn't need them because the URL is pre-signed.
   - Does it support `Range: bytes=N-` (resumable download)?
   - What's the largest `.zup` size you've tested? We expect to ship 50–150 MB packages routinely.

6. **`POST /api/v1/updates/report` — any rate limit or required ordering?** The agent posts `downloading`, then `applying`, then either `success` or `failed` for the same `(appliance_id, release_id)`. Do you accept all four, or only terminal states? Do you reject duplicates or dedupe them?

7. **Error response shape.** When the server rejects a request, the appliance UI surfaces the error verbatim. What field carries the human-readable message?
   - [ ] `{"error": "..."}`
   - [ ] `{"detail": "..."}`
   - [ ] `{"message": "..."}`
   - Whichever one you use, confirm it's used consistently across all endpoints.

8. **HTTP status codes for the obvious cases.** Just confirm the mapping:
   - Bad license key at register → ?
   - Subscription full at register → ?
   - Subscription expired on checkin → ?
   - Revoked appliance on checkin → ?
   - Invalid api_key → ?

---

## B. Cryptographic key alignment

9. **Public key on the server.** Read out the bytes you embedded as the release-signing public key. Should be exactly:

   ```
   -----BEGIN PUBLIC KEY-----
   MCowBQYDK2VwAyEAmsAbeBh+9DH/ejgjsOEUKPNOA13xIj7zSMoqHig+waI=
   -----END PUBLIC KEY-----
   ```

   If anything else is on the server, we have a key-mismatch bug right now and the first signed release would fail to install.

10. **Private key custody.** Confirm the matching private key is **not** on zentryc.com, **not** in any repo, and lives where? (Hardware token / offline encrypted volume / specific build host name.) The build VM and zentryc.com must never both hold the private key — that's the whole point of signing.

11. **Are you ready to verify a manifest server-side at upload time?** I.e., when ops uploads a `.zup` via `/admin/releases`, does the server re-verify the Ed25519 signature against the public key on disk and reject mismatches? If so, we know the build pipeline can't accidentally publish unsigned junk.

---

## C. Subscription & license keys — the parts not in doc 11

12. **License-key format.** What does an end-user actually paste?
    - Length, character set, examples (mask the value).
    - Is the on-the-wire `registration_token` field the literal pasted string, or hashed/wrapped first?

13. **Single-use vs reusable.** Are license keys single-use (one appliance per key, must issue a new one to register a second box) or reusable (one key registers N boxes up to `max_appliances`)? Doc 13 §7.2 assumed single-use; confirm or correct.

14. **Subscription object shape.** Paste the JSON your server returns in the `subscription` field, with one real-but-fake subscription. The appliance reads:

    ```
    id, name, plan, max_appliances, max_devices,
    used_slots, available_slots, is_active,
    is_expired, expires_at, days_remaining
    ```

    Anything missing means the dashboard will show blanks. Anything renamed means the dashboard will show blanks AND the appliance will stop reflecting subscription state.

15. **Slot release on revocation.** When an admin revokes/deactivates an appliance, does that immediately free a slot in `used_slots`, or is there a delay? The customer-facing UX of "I revoked the dead VM, why can't I register the new one?" depends on this.

16. **Expiry behavior.** When a subscription expires, what does the agent see?
    - [ ] checkin still returns `next_action: "none"`, `subscription.is_expired: true` — appliance keeps running, just no updates.
    - [ ] checkin returns 403/401 — appliance surfaces "expired" prominently.
    - We're hoping for the first one. The second would let an expired customer's monitoring go dark, which is bad business.

---

## D. Rollout engine — defaults that show up in customer-visible behavior

17. **What is the default rollout shape for a fresh release?** When ops uploads a `.zup` and clicks "publish", does it auto-create a canary policy, or is the release visible to nobody until ops also clicks "create rollout"? The build script (`scripts/build-release.py`) takes a `--rollout` flag — does that flag still need to drive a rollout creation, or does publishing imply one?

18. **Per-appliance retry cap.** Doc 13 says "3 failed attempts at a release stops further offers of that release." Did you implement that on the server, or is the agent relying on its local history (which we currently don't enforce)? If the latter, we need to add agent-side tracking.

19. **Deterministic bucketing for percentage rollouts.** Confirm `bucket = sha256(appliance_id + ":" + release_id) % 100` — same as doc 11. The fleet status page will be wrong if appliances bucket non-deterministically.

---

## E. TLS, hostnames, and what the OVA bakes

20. **Final hostname.** `zentryc.com` everywhere, or do we need to bake a different one (e.g., `api.zentryc.com`, `updates.zentryc.com`)? Renaming this later is painful because every shipped OVA carries the old one.

21. **TLS certificate.** Public CA (Let's Encrypt / DigiCert / etc) — confirm. The appliance trusts the system CA bundle by default; if you're using a private CA, every appliance has to be re-armed.

22. **HSTS, HTTP/2, max body size.** Just confirm:
    - HTTPS-only (HTTP redirects to HTTPS).
    - Bodies up to ~150 MB accepted on `/admin/releases` upload.
    - No HSTS preload subdomain weirdness that breaks `https://zentryc.com/api/...` from inside the appliance's containerized FastAPI.

---

## F. Things I need *physically* to run a real end-to-end test

To prove the channel works end-to-end I need the server team to give me:

23. **One staging URL** I can hit from my build VM (this machine, IP visible from the server team).

24. **One test subscription** with `max_appliances=3` so I can register/revoke without affecting prod.

25. **Three test license keys** issued against that subscription. (Yes, three — single-use semantics + needing to test re-register means I burn through them fast.)

26. **One published noop release** at version `0.0.1-test` whose manifest is signed with the production private key. Steps in the manifest can be just `[{"type":"health_check","url":"http://localhost:8000/api/v1/system/health","timeout":30}]`. The point is to prove the *channel*, not to actually update anything. Without a published release I cannot test the download/verify/apply path.

27. **Read-only admin credentials** to the staging admin dashboard so I can see my own appliance row, the rollout state, and the audit log entries my smoke test produces.

---

## G. The 10-minute smoke test we'll run together

Once we have items 23–27, I'll run this from the build VM and we should both see consistent state on each side:

```bash
# On the appliance (build VM) — already wired.
sudo /opt/zenplus/venv/bin/python -m updater --register <license-key>   # Check item 23 + 25.
sudo systemctl start zenplus-updater.service                            # First checkin + apply v0.0.1-test.
sudo cat /opt/zenplus/updater/logs/update.log                           # Should end with "Update completed successfully".
cat /opt/zenplus/updater/logs/update-history.json                       # Should have one record, status=success.

# On zentryc.com staging admin dashboard:
# - Appliance row created, last_checkin within last minute.
# - update_history row: appliance_id matches, status=success, from_version=<current>, to_version=0.0.1-test.
# - audit_log: register + checkin + download + report events.
```

If those four pass, we have **proof** the contract works in both directions and we can move on to a real release.

---

## H. Bonus questions (will not block v1 but would be nice to know)

28. **Per-appliance log forwarding.** The agent collects an `update.log` (rotating, 10 MB max). Should it ship that log on `report status=failed` so support has the trace, or do we wait for a customer to upload it manually? Cheapest path is "agent gzips and POSTs the tail-200 lines as `log_data` field on the report" — already in the agent contract.

29. **Audit retention.** How long does zentryc.com keep `update_history` rows? If <90d, we should mirror what we need locally on the appliance.

30. **Multi-region.** Is the server single-region, or fronted by a CDN? If single-region, customers in geographies far from your DC will see slow downloads — we should know so we can set realistic timeouts.

---

## What we're NOT asking and why

- We don't ask about the admin dashboard UX. That's your team's call; the appliance doesn't see it.
- We don't ask about how you generate license keys (random, structured, signed JWT, whatever). The agent just sends the string in `registration_token`. As long as your server can parse it back to a subscription, the wire format is your problem.
- We don't ask about disaster recovery / DB backups for zentryc.com itself. That's an SRE topic for your team.
- We don't ask about pricing or billing integration. Out of scope for the OTA channel.

---

*Document version: 1.0 | Updated: 2026-05-02 | Owner: ZenPlus Engineering*

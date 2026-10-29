# Notification peer credentials

Migration 177 adds expiry, one previous credential during rotation, and durable
revocation to inbound notification trust. New credentials default to 90 days;
the API accepts 1 to 365 days. Existing credentials retain their previous
unlimited lifetime until the administrator rotates them. The page labels those
legacy credentials so they can be scheduled for replacement.

On the sender site, use **Rotate credential** for the allowed origin. Rotation
shows the replacement once and accepts the previous credential for a chosen
0 to 3,600 seconds (default 300). Overlap never extends the old credential's
expiry. A second rotation replaces the previous overlap; coordinate one
rotation at a time. On the origin, use **Update connection** for the sender,
paste the replacement and verify. This retains the peer ID and policy order;
the API verifies authenticated TLS and the same commissioned site identity
before saving encrypted connection data. The existing private CA can be reused
or replaced with a verified CA during certificate renewal.

**Revoke credential** clears both hashes and disables the trust. Re-enabling
alone cannot restore either credential. Issue a replacement, configure the
origin, then explicitly enable a disabled trust. Revocation prevents new
authenticated requests after it commits; a request or provider call already
in progress can finish. It does not recall delivered notifications. A disabled
or expired preferred sender follows the existing failure policy. An uncertain
provider outcome still follows reconciliation or local-safety rules.

All lifecycle operations require `settings.manage` and use the audit log without
credential values. Listing configuration exposes expiry and revocation state,
the public CA and address, but neither credential nor credential hashes. Tokens
are not placed in browser storage or URLs. Deploy the migration before the API
and dashboard on every candidate. Schema rollback is unnecessary for an older
API, but older code does not enforce the new lifecycle: do not downgrade a cell
that depends on expiry or revocation for its security policy.

Qualification must include expiry, bounded overlap, second rotation, revocation
followed by enable, encrypted connection replacement, preserved policy order,
controller failover and a real captured notification. Provider receipts and
retention reconciliation remain separate work; a credential lifecycle test is
not proof of complete appliance acceptance.

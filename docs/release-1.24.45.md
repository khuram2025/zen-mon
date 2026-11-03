# ZenPlus 1.24.45

Fix SMS channels with multiple destination numbers. Each unique number is sent
separately, including channel tests, gateway tests, alert notifications and NCM
notifications. Commas, semicolons and new lines separate numbers; common phone
formatting is removed. Unifonic destinations use international digits without
the leading plus or 00, while other providers retain the plus sign.

Track alert acceptance, rejection and uncertain delivery per recipient. A failed
number does not stop later numbers; retries do not repeat accepted or uncertain
recipients. NCM outbox retries retain partial progress. Channel tests report the
number accepted and report partial failures instead of claiming complete success.

Preserve the configured gateway endpoint, credentials, email routing, SMS event
timestamps and the controller restart protection from 1.24.44. No schema migration.

Appliance-only release. Requires the signed 1.24.36 installer catalog bridge.

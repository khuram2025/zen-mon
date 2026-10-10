# Release 1.24.7 — SMS channel delivery and gateway selection

SMS channel tests and alert delivery now use the same HTTP request builder as gateway tests. Form requests carry the configured content type and encode substituted values; JSON strings are escaped before parsing. Basic and bearer authentication are consistent across gateway/channel tests, alert delivery, simulation, and configuration-notification SMS. Invalid JSON fails before sending, and a failed request is not automatically retried as a second POST.

Tests only report provider acceptance for a successful HTTP response without an explicit `success`/`Success` rejection. HTTP 402 and other provider failures produce a clear test error, without exposing response bodies, recipients, or credentials. Unsupported providers do not produce a misleading successful test.

The channel editor initializes from the saved channel before mounting its controls, preserves the authoritative saved gateway link, and ignores empty initialization events from the form select. Opening, saving, and reopening an SMS channel keeps its explicit gateway. Clicking the already selected channel type does not reset configuration. Default selection remains intentional and names the current default; an unavailable explicit selection is visible and channel tests do not silently fall back to another gateway.

No database migrations or monitoring-history deletion are included. The appliance-only update retains the existing Windows agent distribution and storage/flow-history performance changes.

Validation: isolated HTTP transport regressions exercise content types, encoding, authentication, selected/disabled/missing gateways, HTTP 402, provider-level failures, malformed JSON, and single-attempt behavior. Development UI acceptance uses synthetic gateway records, with outbound operations mocked. Release acceptance compares signed inventories, service health, table integrity, saved monitoring history, and exact notification-configuration fingerprints. Production SMS acceptance uses only the owner-approved single test through the existing SMS channel.

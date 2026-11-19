# ZenPlus 1.25.9

A shared alert rule (one that applies to many devices or servers, such as
"Server restart" or "Device Down") can now be turned off for a single device
or server. Before, the switch on a server's Alerts tab disabled the rule for
every server.

- Server -> Alerts: a global rule's switch turns it off/on for this server only,
  with an optional reason; the row shows who disabled it and when.
- Device -> Configuration: new "Alert rules" panel with the same switch.
- Alert detail -> Policy: "Off for this device/server" next to "Disable for all".
- Alert Rules: "Off for N" lists the excluded assets (linked, who, when, why)
  with Re-enable.

Turning a rule off for an asset resolves that rule's open alerts there without a
recovery notice; turning it back on resumes evaluation on the next cycle.
Changing exclusions needs alerts.manage and the asset in the user's scope.

Migration 187 adds `alert_rule_exclusions`. Also contains 1.25.7 (one-click
site detail maps) and 1.25.8 (update wheels bundled for appliances without
package-index access), which were not published separately.

Appliance-only package; minimum upgrade base 1.24.36; signed Windows agent
catalog preserved. Details: docs/alert-rule-exclusions.md.

# ZenPlus 1.25.16

Fixes to discovery rules from a functional test on a live appliance.

- Growth stop: an automatic run is held only when a rule newly matches many
  more devices than before. Adding an action to a rule that matches the same
  devices no longer holds the run for approval; the per-run limits still apply.
- Conditions: a yes/no fact a device or scan result does not have (for example
  "ping on" for a scan result) now satisfies neither "is true" nor "is false",
  like every other condition. Device group rules are unchanged.
- Agent policy: a rule no longer applies a policy made for another platform (a
  Windows policy to a Linux agent); the run lists it with the reason.
- Wording in the rule editor and rules list.

No migration. Includes 1.25.15. Appliance-only package; minimum upgrade base
1.24.36; signed Windows agent catalog preserved. Details:
docs/discovery-rule-engine.md.

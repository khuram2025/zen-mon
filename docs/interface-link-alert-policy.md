# Interface link alerts

The default Interface down rule covers infrastructure links after 180 seconds
of continuously observed down state. It uses target `role:infrastructure`:

- Uplinks and ports with no Device Tracker classification remain eligible.
- Ports classified as access/non-uplink are excluded from this rule.
- An interface tagged `link-critical` remains eligible, including an access port
  connecting a critical server, camera, firewall or other endpoint.
- Device Tracker's manual uplink/access override takes precedence over discovery.
- Positive uplink evidence is retained by device and stable interface name, so
  losing LLDP/FDB during an outage does not remove alert coverage. Review a
  repurposed port with the manual access override.

This policy does not disable monitoring or discard interface history. Separate
custom rules with a port name, description, alias or index target can still
monitor any access port. Unknown roles remain covered until reviewed.

When an existing incident is excluded by this policy, it is closed with
`resolved_by=policy_scope_change`, explicitly labelled as a policy change rather
than a link recovery. No recovery notification is sent. Historical severity and
the original policy snapshot remain intact.

Migration 186 adjusts only the original unscoped, zero-delay Critical Interface
down rule. It preserves custom targets, thresholds, scopes, conditions and holds.
Review critical endpoint ports with the network team and add `link-critical`
where link loss should page even though the port is an access port.

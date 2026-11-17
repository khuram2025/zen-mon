# ZenPlus 1.25.15

Discovery rules can now manage interfaces, follow the network around a device,
place devices on remote sensors, set server agent policies and run on a
schedule.

- Interface rules: monitor or ignore interfaces by name, alias, description,
  interface type or speed (for example ignore loopbacks and VLAN interfaces,
  monitor every port whose alias says "uplink"). For each interface the first
  matching pattern decides; interfaces no pattern matches are left alone.
  New interfaces are picked up automatically, and undo puts each interface
  back as it was. An *ignore* pattern is the only way a rule turns monitoring
  off, and only for the interfaces it names.
- Neighbour conditions: "has LLDP/CDP neighbour", neighbour count and
  "depends on". A neighbour counts only when the link was seen in the last
  24 hours with both ports up, the same rule network maps use.
- Sensors and sites: conditions on the sensors and sites that monitor a
  device or the site of a server, and a "Monitor from sensor" action. It adds
  the sensor only when it is online and authorised; otherwise the change
  waits and is tried again. It never removes a monitoring location.
- Servers: an "Agent policy" action (servers without an agent are skipped),
  and conditions on agent policy, software baselines and baseline
  compliance. Baselines are assigned by tag, which rules already set.
- "Add service check from template": creates the template's check for the
  device once. Undo never deletes a created check.
- Scheduled runs: rules can run over the whole inventory at set times (every
  1 to 168 hours, local time zone), plus "Run scheduled rules now". Off until
  turned on in the engine settings.
- Safety: a rule can have its own limit on changes per automatic run, and a
  growth stop holds an automatic run for approval when one rule suddenly
  changes far more devices than it matched last time (50 %, at least 20
  assets, adjustable or off). The approval view says why a run waits.
- Unmatched report: devices, servers and managed switches/APs no active rule
  matches.

The engine stays off until turned on under Discovery -> Rules. No migration.
On an appliance with active "whenever they change" rules, the first pass
after the upgrade re-checks every asset once. Includes 1.25.14.
Appliance-only package; minimum upgrade base 1.24.36; signed Windows agent
catalog preserved. Details: docs/discovery-rule-engine.md.

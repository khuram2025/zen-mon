# ZenPlus 1.25.10

Device Templates: the monitoring templates become a managed object with a type
catalogue, history, drafts, pilots and safe customisation of built-ins.

- Network -> Device Templates replaces Settings -> Monitoring Templates: list,
  form editor with an OID search in the MIB Library, devices with metric
  coverage, history, unmatched devices and suggestions.
- Edits are saved as a draft and devices keep polling the published revision.
  Publish shows the impact first (groups and metrics added or removed, OIDs
  per poll, alert rules that use a removed metric) and goes to every device or
  to a pilot of chosen devices. Every published revision can be rolled back.
- Customise a built-in template: only your changes are stored, and the vendor
  updates that ship with releases still arrive.
- Test a template on a device before publishing (nothing is stored).
- Device types are a catalogue: load balancer, wireless controller, SD-WAN
  edge, storage, UPS, PDU, IP camera and VoIP phone join the original seven,
  and administrators can add their own.
- Classification: match priority and "require sysDescr match" for models that
  share a sysObjectID. A device that already has a template is never moved;
  when another template matches better it is listed under Suggestions.
- Devices list: bulk "Template" action. Device page: the template links to the
  template and says who assigned it.

New permission devices.manage_templates (administrators, Operator and Network
Team). A custom role that edited templates through devices.manage needs it
added. Users limited to tagged devices cannot change templates.

Migration 189 adds device_types and template history and replaces the old
device type CHECK. Existing templates, devices and readings are unchanged and
nothing is re-classified by the upgrade.

Appliance-only package; minimum upgrade base 1.24.36; signed Windows agent
catalog preserved. Details: docs/device-templates.md.

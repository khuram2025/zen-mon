# ZenPlus 1.25.17

Alert thresholds per device class, and fixes to users and Settings.

- Templates recommend alert rules: every template metric with warning and
  critical levels, plus uplink and WAN link rules on the built-in network
  templates. Nothing is created by the upgrade; Accept on the template's
  Recommended alerts tab creates an ordinary rule for the devices on that
  template. A later template revision never changes an accepted rule: the page
  shows the difference with Apply or Keep.
- Overrides: change a rule's threshold, clear level, holds or priority for one
  device, server or group, optionally for some interfaces, components or mount
  points only, with a reason and an optional expiry. The first matching
  override wins. Turning a rule off for a device stays an exclusion.
- Device page: an Alert thresholds card lists every rule that covers the
  device with the values in force and where they come from (rule, override,
  excluded, off). Template metric colours follow the accepted rule's levels.
- Interface roles (uplink, WAN, access, management, tunnel and so on) from the
  template, changeable per interface, usable as an alert rule condition. Roles
  never change whether an interface is monitored.
- Fixed: the role of a user who signs in through Active Directory / LDAP,
  RADIUS or OIDC could not be changed. Edit user now has "Role is decided by":
  the directory's role mapping, or an administrator. A role set by hand
  survives sign-in until the account is handed back to the directory; the
  directory still decides whether the account may sign in.
- Fixed: sign-in matched usernames by exact case, so a second account could be
  created for the same directory user. Sign-in now ignores case.
- Fixed: Settings -> SMTP / Email could put back an old mail server after a
  change on the Gateways page. It now edits the gateway in use and never
  returns the password.
- Fixed: saving update settings no longer starts an updater timer that was
  stopped on purpose.
- Fixed: Settings -> Authentication refuses to disable the only source every
  administrator signs in through, and no longer shows a successful connection
  step for an unreachable LDAP server.
- Further fixes across the Settings tabs: permissions that did not match the
  page, forms that could overwrite settings after a failed load, and old
  /settings links.

Migrations 193 and 195. Includes 1.25.16. Appliance-only package; minimum
upgrade base 1.24.36; signed Windows agent catalog preserved. Details:
docs/alert-rule-overrides.md, docs/directory-account-roles.md.

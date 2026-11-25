# ZenPlus 1.25.30

Service checks can be told which address to connect to for a host name, and
the certificates they trust are now easy to find.

Manual host addresses
- Settings -> General -> Monitoring has a new section, Service check
  connections. An administrator lists a host name and the address service
  checks connect to for it: everywhere, on this appliance only, or on one
  remote sensor.
- Use it when a name is not in the DNS the appliance uses, or when a name
  must be checked at one specific address.
- Only the address the check connects to changes. The URL, the Host header
  and the certificate name check keep the name, so a certificate issued for
  another name still fails.
- HTTP(S), TLS certificate, TCP port and ping checks use it. DNS checks never
  do: they test name resolution itself.
- A check that used a manual address says so in its result and on its detail
  page, with the address, so it is never mistaken for working DNS.
- It is not the system hosts file: updates, licensing, mail and directory
  sign-in are not affected.
- A remote sensor too old to apply manual addresses is shown as such instead
  of silently ignoring them.

Trusted certificates
- The same section summarises the certificates service checks trust and
  links to Security & TLS -> Service probe trust, where they are managed.
- When a check test fails on certificate trust or on name resolution, the
  test result says so and links to the right setting.

Notes for the upgrade
- Nothing changes for existing checks: no manual address exists until an
  administrator adds one.
- Adding, changing and removing manual addresses needs an administrator and
  is written to the audit log.

Includes everything in 1.25.29. Migration 215. Appliance-only package;
minimum upgrade base 1.24.36; signed Windows agent catalog preserved.
Details: PR #202.

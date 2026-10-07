# ZenPlus 1.23.30 — SSH terminal in Settings

Settings > General > Monitoring has a new **SSH terminal** card: enter a host,
port, username and password and an interactive shell opens in the browser, run
from the appliance, to test reachability, logins and commands on a device.

- Requires the `settings.manage` permission. Sessions are private to the user
  who opened them (at most 3 at a time), close after 10 minutes without input
  or 60 minutes in total, and can be closed with Disconnect.
- The password is sent once to sign in. It is never stored or logged. The audit
  log records who opened a session, to which host and account, and when it ended.
- The device host key is not checked (same policy as NCCM since 1.23.29).
- Built on plain HTTP (streamed output plus input requests, bridged through
  Redis between API workers) so it works through the stock nginx configuration;
  no WebSocket or nginx change is needed.
- Wrong credentials, unreachable hosts and timeouts show a clear message and do
  not end your dashboard login.

Adds the `@xterm/xterm` and `@xterm/addon-fit` dashboard libraries. No
migrations. Includes 1.23.29 (SSH host trust removed). Appliance-only,
minimum prerequisite 1.23.9.

# ZenPlus 1.23.33 — Server Compliance tab shows only applicable baselines

The server Compliance tab no longer lists results for baselines that do not
currently apply to that server (disabled baselines, baselines rescoped to another
OS/site/tags, or decommissioned servers). Those stale rows used to remain until
the server happened to be re-evaluated, so the tab disagreed with the Baselines
page.

- The tab and its badge count share one scope check with the evaluator.
- Rescoping or disabling a baseline now re-evaluates every server that already
  holds results for it, clearing stale rows and their alerts.
- Editing a server's OS, site, tags or status re-evaluates its baselines.
- An hourly, advisory-locked repair pass re-evaluates all servers.

No schema changes. Appliance-only, minimum prerequisite 1.23.9.

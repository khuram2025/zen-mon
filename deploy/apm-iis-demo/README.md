# ZenPlus IIS APM demo

This isolated ASP.NET Framework application is deployed at
`/ZenPlusApmDemo/` on the test IIS server. It provides controlled success,
latency, dependency and exception endpoints for APM validation. The page is
clearly labelled as demo telemetry and does not modify `LocalAuthTest`.

The deployment may inject a controller-hosted RUM SDK tag. Never place a
secret SDK ingest key in this page; only a public, exact-origin `zpr_` RUM key
is appropriate for browser code.

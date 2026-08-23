<%@ Page Language="C#" %>
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ZenPlus IIS APM Demo</title>
  <style>
    body { margin: 0; font-family: "Segoe UI", sans-serif; background: #0b1220; color: #e5edf8; }
    main { max-width: 920px; margin: 48px auto; padding: 0 24px; }
    .card { background: #111c30; border: 1px solid #243653; border-radius: 16px; padding: 24px; box-shadow: 0 18px 50px #02061766; }
    h1 { margin-top: 0; font-size: 28px; } p { color: #aebed4; line-height: 1.6; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 12px; margin-top: 22px; }
    button { width: 100%; border: 0; border-radius: 10px; padding: 13px; background: #2463eb; color: white; font-weight: 650; cursor: pointer; }
    button.slow { background: #b7791f; } button.error { background: #c53030; } button.dependency { background: #087f5b; }
    pre { min-height: 72px; margin-top: 18px; padding: 14px; overflow: auto; border-radius: 10px; background: #07101f; color: #93c5fd; }
    .badge { display: inline-block; padding: 4px 9px; border-radius: 99px; background: #123b2d; color: #6ee7b7; font-size: 12px; font-weight: 700; }
  </style>
</head>
<body>
<main>
  <div class="card">
    <span class="badge">DEMO TELEMETRY</span>
    <h1>ZenPlus IIS APM validation</h1>
    <p>This isolated ASP.NET application produces real IIS transactions for validating traces, latency, errors, dependencies, browser RUM and database insights.</p>
    <div class="grid">
      <button onclick="run('success')">Successful request</button>
      <button class="slow" onclick="run('slow')">Slow transaction</button>
      <button class="dependency" onclick="run('dependency')">Dependency call</button>
      <button class="error" onclick="run('error')">Controlled error</button>
    </div>
    <pre id="result">Ready.</pre>
  </div>
</main>
<script>
async function run(action) {
  const out = document.getElementById('result');
  const started = performance.now();
  try {
    const response = await fetch('Api.aspx?action=' + encodeURIComponent(action));
    const text = await response.text();
    out.textContent = response.status + ' in ' + Math.round(performance.now() - started) + ' ms\n' + text;
  } catch (error) { out.textContent = String(error); }
}
</script>
<!-- The deployment replaces this placeholder with the controller-hosted RUM SDK tag. -->
__ZENPLUS_RUM_SCRIPT__
</body>
</html>

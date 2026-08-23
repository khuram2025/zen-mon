$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

Import-Module WebAdministration

$target = 'C:\inetpub\wwwroot\ZenPlusApmDemo'
$backupRoot = 'C:\ProgramData\ZenPlus\Agent\backups'
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

if (Test-Path -LiteralPath $target) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    Copy-Item -LiteralPath $target `
        -Destination (Join-Path $backupRoot "ZenPlusApmDemo-$stamp") `
        -Recurse -Force
}

New-Item -ItemType Directory -Path $target -Force | Out-Null

$defaultPage = @'
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
__ZENPLUS_RUM_SCRIPT__
</body>
</html>
'@

$apiPage = @'
<%@ Page Language="C#" %>
<%@ Import Namespace="System" %>
<%@ Import Namespace="System.Net" %>
<%@ Import Namespace="System.Threading" %>
<script runat="server">
protected void Page_Load(object sender, EventArgs e)
{
    Response.ContentType = "application/json";
    Response.Cache.SetCacheability(HttpCacheability.NoCache);
    string action = (Request.QueryString["action"] ?? "success").ToLowerInvariant();
    if (action == "slow")
    {
        Thread.Sleep(850);
        Response.Write("{\"ok\":true,\"operation\":\"slow\",\"delay_ms\":850}");
        return;
    }
    if (action == "dependency")
    {
        using (var client = new WebClient())
        {
            client.UseDefaultCredentials = true;
            client.Headers[HttpRequestHeader.UserAgent] = "ZenPlus-IIS-APM-Demo/1.0";
            string body = client.DownloadString("http://127.0.0.1/LocalAuthTest/");
            Response.Write("{\"ok\":true,\"operation\":\"dependency\",\"bytes\":" + body.Length + "}");
        }
        return;
    }
    if (action == "error")
    {
        throw new InvalidOperationException("ZenPlus controlled demo exception");
    }
    Thread.Sleep(25);
    Response.Write("{\"ok\":true,\"operation\":\"success\",\"server\":\"" + Environment.MachineName + "\"}");
}
</script>
'@

$webConfig = @'
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <system.web>
    <compilation debug="false" targetFramework="4.8" />
    <httpRuntime targetFramework="4.8" />
    <customErrors mode="Off" />
  </system.web>
  <system.webServer>
    <defaultDocument enabled="true">
      <files>
        <clear />
        <add value="Default.aspx" />
      </files>
    </defaultDocument>
    <httpErrors errorMode="DetailedLocalOnly" existingResponse="PassThrough" />
  </system.webServer>
</configuration>
'@

$utf8 = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $target 'Default.aspx'), $defaultPage, $utf8)
[IO.File]::WriteAllText((Join-Path $target 'Api.aspx'), $apiPage, $utf8)
[IO.File]::WriteAllText((Join-Path $target 'web.config'), $webConfig, $utf8)

if (-not (Get-WebApplication -Site 'Default Web Site' -Name 'ZenPlusApmDemo' -ErrorAction SilentlyContinue)) {
    New-WebApplication -Site 'Default Web Site' -Name 'ZenPlusApmDemo' `
        -PhysicalPath $target -ApplicationPool 'DefaultAppPool' | Out-Null
}

Restart-WebAppPool -Name 'DefaultAppPool'
Start-Sleep -Seconds 3

$results = foreach ($action in @('success', 'slow', 'dependency', 'error')) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -UseDefaultCredentials `
            -Uri "http://127.0.0.1/ZenPlusApmDemo/Api.aspx?action=$action" `
            -TimeoutSec 15
        $status = [int]$response.StatusCode
    }
    catch {
        $status = [int]$_.Exception.Response.StatusCode.value__
    }
    [pscustomobject]@{ Action = $action; Status = $status }
}

[pscustomobject]@{
    Application = 'Default Web Site/ZenPlusApmDemo'
    Pool = (Get-WebAppPoolState -Name 'DefaultAppPool').Value
    Results = @($results)
    PhysicalPath = $target
} | ConvertTo-Json -Compress -Depth 5

[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$PythonPath,

  [Parameter(Mandatory = $true)]
  [ValidatePattern('^[^\r\n,=]{1,255}$')]
  [string]$ServiceName,

  [ValidatePattern('^[^\r\n,=]{1,64}$')]
  [string]$Environment = "prod"
)

$ErrorActionPreference = "Stop"
$wheelhouse = Join-Path $PSScriptRoot "wheelhouse"
$constraints = Join-Path $PSScriptRoot "constraints.txt"
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
  throw "Python executable was not found: $PythonPath"
}
if (-not (Test-Path -LiteralPath $wheelhouse -PathType Container)) {
  throw "ZenPlus Python wheelhouse is missing: $wheelhouse"
}
if (-not (Test-Path -LiteralPath $constraints -PathType Leaf)) {
  throw "ZenPlus Python dependency constraints are missing: $constraints"
}

$runtime = (& $PythonPath -c 'import platform, struct, sys; print(f"{platform.python_implementation()}|{sys.version_info.major}.{sys.version_info.minor}|{struct.calcsize(''P'') * 8}")' 2>&1 | Out-String).Trim()
$runtimeParts = $runtime.Split('|')
if ($LASTEXITCODE -ne 0 -or $runtimeParts.Count -ne 3 -or
    $runtimeParts[0] -ne "CPython" -or
    $runtimeParts[1] -notin @("3.10", "3.11", "3.12", "3.13") -or
    $runtimeParts[2] -ne "64") {
  throw "ZenPlus offline Python tracing supports CPython 3.10-3.13 x64; detected '$runtime'"
}

& $PythonPath -m pip --version | Out-Null
if ($LASTEXITCODE -ne 0) {
  throw "The selected Python environment does not provide pip"
}
& $PythonPath -m pip install --disable-pip-version-check --no-index --find-links $wheelhouse `
  --constraint $constraints "opentelemetry-distro==0.65b0" "opentelemetry-exporter-otlp-proto-http==1.44.0"
if ($LASTEXITCODE -ne 0) {
  throw "Unable to install the ZenPlus OpenTelemetry Python base packages"
}

$scriptsDirectory = (& $PythonPath -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2>&1 | Out-String).Trim()
$bootstrap = Join-Path $scriptsDirectory "opentelemetry-bootstrap.exe"
$instrument = Join-Path $scriptsDirectory "opentelemetry-instrument.exe"
if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf) -or -not (Test-Path -LiteralPath $instrument -PathType Leaf)) {
  throw "OpenTelemetry Python console tools were not installed into $scriptsDirectory"
}
$requested = @(& $bootstrap -a requirements 2>&1)
if ($LASTEXITCODE -ne 0) {
  throw "Unable to discover Python instrumentation requirements: $($requested -join [Environment]::NewLine)"
}
$available = New-Object Collections.Generic.List[string]
$skipped = New-Object Collections.Generic.List[string]
foreach ($requirement in $requested) {
  $requirement = [string]$requirement
  if ($requirement -notmatch '^\s*([A-Za-z0-9_.-]+)') { continue }
  $packageName = $Matches[1]
  $wheelPrefix = ($packageName.ToLowerInvariant() -replace '[-.]', '_') + "-"
  $match = Get-ChildItem -LiteralPath $wheelhouse -Filter "*.whl" -File |
    Where-Object { $_.Name.ToLowerInvariant().StartsWith($wheelPrefix) } |
    Select-Object -First 1
  if ($null -ne $match) {
    $available.Add($requirement.Trim())
  } else {
    $skipped.Add($requirement.Trim())
  }
}

if ($available.Count -gt 0) {
  $requirementsPath = Join-Path ([IO.Path]::GetTempPath()) ("zenplus-python-apm-" + [Guid]::NewGuid().ToString("N") + ".txt")
  try {
    [IO.File]::WriteAllLines($requirementsPath, $available, (New-Object Text.UTF8Encoding($false)))
    & $PythonPath -m pip install --disable-pip-version-check --no-index --find-links $wheelhouse `
      --constraint $constraints -r $requirementsPath
    if ($LASTEXITCODE -ne 0) {
      throw "Unable to install one or more ZenPlus Python instrumentation packages"
    }
  }
  finally {
    Remove-Item -LiteralPath $requirementsPath -Force -ErrorAction SilentlyContinue
  }
}
& $PythonPath -m pip check
if ($LASTEXITCODE -ne 0) {
  throw "The selected Python environment has dependency conflicts after instrumentation installation"
}

Write-Host "ZenPlus Python tracing packages installed successfully."
Write-Host "Configure the application process with:"
Write-Host "  OTEL_SERVICE_NAME=$ServiceName"
Write-Host "  OTEL_RESOURCE_ATTRIBUTES=deployment.environment=$Environment"
Write-Host "  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318"
Write-Host "  OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf"
Write-Host "  OTEL_TRACES_EXPORTER=otlp"
Write-Host "  OTEL_METRICS_EXPORTER=none"
Write-Host "  OTEL_LOGS_EXPORTER=none"
Write-Host "Then launch through: $instrument <application arguments>"
if ($skipped.Count -gt 0) {
  Write-Warning ("No bundled offline wheel is available for: " + ($skipped -join ", "))
}

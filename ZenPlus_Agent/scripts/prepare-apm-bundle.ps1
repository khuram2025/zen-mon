param(
  [switch]$Force,
  [switch]$SkipLanguagePacks,
  [string]$PythonPath = $env:ZENPLUS_BUILD_PYTHON
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path "$PSScriptRoot\.."
$cache = Join-Path $root ".tools\apm"
$stage = Join-Path $cache "stage"
$go = Join-Path $root ".tools\go\bin\go.exe"
$python = $PythonPath
if (-not $python) {
  $portablePython = Join-Path $root ".tools\python-nuget\package\tools\python.exe"
  if (Test-Path $portablePython) {
    $python = $portablePython
  } else {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
      $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    }
    if ($null -ne $pythonCommand -and $pythonCommand.Path -notlike "*\Microsoft\WindowsApps\*") {
      $python = $pythonCommand.Path
    }
  }
}

if (-not $python) {
  # The CPython-owned NuGet package is a portable build dependency. It is
  # pinned by SHA-256 and stays under .tools; it is not shipped in the agent.
  $pythonPackageUrl = "https://api.nuget.org/v3-flatcontainer/python/3.12.10/python.3.12.10.nupkg"
  $pythonPackageSHA256 = "0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8"
  $pythonPackageDir = Join-Path $root ".tools\python-nuget"
  $pythonPackage = Join-Path $pythonPackageDir "python.3.12.10.nupkg"
  $pythonPackageExtract = Join-Path $pythonPackageDir "package"
  New-Item -ItemType Directory -Force -Path $pythonPackageDir | Out-Null
  if (-not (Test-Path $pythonPackage)) {
    Invoke-WebRequest -Uri $pythonPackageUrl -OutFile $pythonPackage
  }
  $actualPythonPackageSHA256 = (Get-FileHash -Algorithm SHA256 $pythonPackage).Hash.ToLowerInvariant()
  if ($actualPythonPackageSHA256 -ne $pythonPackageSHA256) {
    throw "SHA-256 mismatch for pinned CPython build dependency"
  }
  if (-not (Test-Path $pythonPackageExtract)) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($pythonPackage, $pythonPackageExtract)
  }
  $python = Join-Path $pythonPackageExtract "tools\python.exe"
  if (-not (Test-Path $python)) {
    throw "Pinned CPython build dependency did not contain tools\python.exe"
  }
}

if (-not (Test-Path $go)) {
  throw "Portable Go toolchain is missing: $go"
}

function Get-VerifiedFile {
  param(
    [string]$Url,
    [string]$Destination,
    [string]$Sha256
  )
  if ($Force -or -not (Test-Path $Destination)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $Destination) | Out-Null
    Invoke-WebRequest -Uri $Url -OutFile $Destination
  }
  $actual = (Get-FileHash -Algorithm SHA256 $Destination).Hash.ToLowerInvariant()
  if ($actual -ne $Sha256.ToLowerInvariant()) {
    throw "SHA-256 mismatch for $Destination (expected $Sha256, got $actual)"
  }
}

New-Item -ItemType Directory -Force -Path $cache | Out-Null

$ocb = Join-Path $cache "ocb-0.158.0.exe"
Get-VerifiedFile `
  -Url "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/cmd/builder/v0.158.0/ocb_0.158.0_windows_amd64.exe" `
  -Destination $ocb `
  -Sha256 "8fc6f263d0d251ea1497e32386f69924c5f7fef4cde948afb3dc7183b2966fc3"

$collectorOut = Join-Path $cache "collector-build"
$collector = Join-Path $collectorOut "zenplus-telemetry-gateway.exe"
if ($Force -or -not (Test-Path $collector)) {
  $env:GOTMPDIR = Join-Path $root ".gotmp"
  New-Item -ItemType Directory -Force -Path $env:GOTMPDIR | Out-Null
  $previousPath = $env:PATH
  $env:PATH = (Split-Path $go) + ";" + $env:PATH
  Push-Location $root
  try {
    & $ocb --config "packaging\apm-collector-builder.yaml"
    if ($LASTEXITCODE -ne 0) {
      throw "OpenTelemetry Collector Builder failed with exit code $LASTEXITCODE"
    }
  }
  finally {
    Pop-Location
    $env:PATH = $previousPath
  }
}

if (-not (Test-Path $collector)) {
  throw "Custom telemetry gateway was not produced: $collector"
}

if (Test-Path $stage) {
  $resolvedStage = (Resolve-Path $stage).Path
  $resolvedCache = (Resolve-Path $cache).Path
  if (-not $resolvedStage.StartsWith($resolvedCache, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to replace stage outside the APM cache: $resolvedStage"
  }
  Remove-Item -Recurse -Force -LiteralPath $resolvedStage
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stage "gateway") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stage "instrumentation") | Out-Null
Copy-Item -Force $collector (Join-Path $stage "gateway\zenplus-telemetry-gateway.exe")
Copy-Item -Force (Join-Path $root "packaging\THIRD-PARTY-NOTICES.txt") (Join-Path $stage "THIRD-PARTY-NOTICES.txt")

$components = @(
  [ordered]@{
    name = "zenplus-telemetry-gateway"
    version = "0.158.0-zp1"
    source = "OpenTelemetry Collector 0.158.0"
    sha256 = (Get-FileHash -Algorithm SHA256 $collector).Hash.ToLowerInvariant()
  }
)

if (-not $SkipLanguagePacks) {
  $dotnetZip = Join-Path $cache "opentelemetry-dotnet-instrumentation-windows-1.16.0.zip"
  Get-VerifiedFile `
    -Url "https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation/releases/download/v1.16.0/opentelemetry-dotnet-instrumentation-windows.zip" `
    -Destination $dotnetZip `
    -Sha256 "2879cf42757030c4421b1049a66283ac41eb3971133ead6c992af889321a2ea6"
  $dotnetDir = Join-Path $stage "instrumentation\dotnet"
  Expand-Archive -Force -Path $dotnetZip -DestinationPath $dotnetDir
  $components += [ordered]@{
    name = "opentelemetry-dotnet-auto"
    version = "1.16.0"
    source = "https://github.com/open-telemetry/opentelemetry-dotnet-instrumentation"
    sha256 = "2879cf42757030c4421b1049a66283ac41eb3971133ead6c992af889321a2ea6"
  }

  $javaJar = Join-Path $stage "instrumentation\java\opentelemetry-javaagent.jar"
  New-Item -ItemType Directory -Force -Path (Split-Path $javaJar) | Out-Null
  Get-VerifiedFile `
    -Url "https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/download/v2.31.0/opentelemetry-javaagent.jar" `
    -Destination $javaJar `
    -Sha256 "d48673b2ff956b26d809bc34243649913d4eefd9191c4e175b686da633e0134b"
  $components += [ordered]@{
    name = "opentelemetry-javaagent"
    version = "2.31.0"
    source = "https://github.com/open-telemetry/opentelemetry-java-instrumentation"
    sha256 = "d48673b2ff956b26d809bc34243649913d4eefd9191c4e175b686da633e0134b"
  }

  $nodeDir = Join-Path $cache "node-bundle"
  $nodeSpec = Join-Path $root "packaging\apm-node"
  if (-not (Test-Path (Join-Path $nodeSpec "package-lock.json"))) {
    throw "The locked Node.js runtime pack is missing: $nodeSpec\package-lock.json"
  }
  if (Test-Path $nodeDir) {
    $resolvedNodeDir = (Resolve-Path $nodeDir).Path
    $resolvedCache = (Resolve-Path $cache).Path
    if (-not $resolvedNodeDir.StartsWith($resolvedCache, [StringComparison]::OrdinalIgnoreCase)) {
      throw "Refusing to replace Node.js cache outside the APM cache: $resolvedNodeDir"
    }
    Remove-Item -Recurse -Force -LiteralPath $resolvedNodeDir
  }
  New-Item -ItemType Directory -Force -Path $nodeDir | Out-Null
  Copy-Item -Force (Join-Path $nodeSpec "package.json") $nodeDir
  Copy-Item -Force (Join-Path $nodeSpec "package-lock.json") $nodeDir
  & npm.cmd ci --prefix $nodeDir --omit=dev --ignore-scripts --no-audit --no-fund
  if ($LASTEXITCODE -ne 0) {
    throw "npm ci failed while preparing the locked Node.js automatic instrumentation pack"
  }
  $nodeTarget = Join-Path $stage "instrumentation\node"
  New-Item -ItemType Directory -Force -Path $nodeTarget | Out-Null
  Copy-Item -Recurse -Force (Join-Path $nodeDir "node_modules") $nodeTarget
  Copy-Item -Force (Join-Path $nodeDir "package-lock.json") $nodeTarget
  Copy-Item -Force (Join-Path $nodeSpec "bootstrap.js") $nodeTarget
  $components += [ordered]@{
    name = "opentelemetry-node-auto"
    version = "0.79.0"
    source = "https://www.npmjs.com/package/@opentelemetry/auto-instrumentations-node"
    sha256 = (Get-FileHash -Algorithm SHA256 (Join-Path $nodeDir "package-lock.json")).Hash.ToLowerInvariant()
  }

  if (-not $python -or -not (Test-Path $python)) {
    throw "Python was not found. Install Python 3 or set ZENPLUS_BUILD_PYTHON to a Python executable."
  }
  $wheelhouse = Join-Path $stage "instrumentation\python\wheelhouse"
  New-Item -ItemType Directory -Force -Path $wheelhouse | Out-Null
  foreach ($pyVersion in @("310", "311", "312", "313")) {
    & $python -m pip download --disable-pip-version-check --dest $wheelhouse `
      --only-binary=:all: --platform win_amd64 --implementation cp --python-version $pyVersion `
      "opentelemetry-distro==0.65b0" "opentelemetry-exporter-otlp-proto-http==1.44.0"
    if ($LASTEXITCODE -ne 0) {
      throw "pip failed while preparing the Python $pyVersion instrumentation wheelhouse"
    }
  }
  $components += [ordered]@{
    name = "opentelemetry-python-auto"
    version = "0.65b0/1.44.0"
    source = "https://pypi.org/project/opentelemetry-distro/"
    sha256 = "recorded-per-wheel-in-sbom"
  }
}

$stagePrefix = [IO.Path]::GetFullPath($stage).TrimEnd('\') + '\'
$files = Get-ChildItem -Path $stage -Recurse -File | Sort-Object FullName | ForEach-Object {
  $fullName = [IO.Path]::GetFullPath($_.FullName)
  if (-not $fullName.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "APM bundle file escaped the staging directory: $fullName"
  }
  [ordered]@{
    # Windows PowerShell 5.1 runs on .NET Framework, which does not expose
    # Path.GetRelativePath. The checked prefix keeps this deterministic and
    # prevents a manifest entry from escaping the signed bundle root.
    path = $fullName.Substring($stagePrefix.Length).Replace("\", "/")
    size = [int64]$_.Length
    sha256 = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLowerInvariant()
  }
}
$manifest = [ordered]@{
  schema_version = 1
  generated_at = (Get-Date).ToUniversalTime().ToString("o")
  components = $components
  files = $files
}
$manifestPath = Join-Path $stage "bundle-manifest.json"
$json = $manifest | ConvertTo-Json -Depth 8
[IO.File]::WriteAllText($manifestPath, $json, (New-Object Text.UTF8Encoding($false)))

$size = (Get-ChildItem -Path $stage -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host "ZenPlus APM bundle ready: $stage ($([math]::Round($size / 1MB, 1)) MB)"

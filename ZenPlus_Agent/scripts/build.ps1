param(
  # The controller address may be compiled into a release for convenience.
  # Enrollment credentials are never compiled into or patched into artifacts.
  [string]$ControllerUrl = $env:ZENPLUS_EMBED_CONTROLLER_URL,
  [string]$SigningThumbprint = $env:ZENPLUS_SIGNING_THUMBPRINT,
  [string]$TimestampUrl = $env:ZENPLUS_TIMESTAMP_URL,
  [string]$PublicBaseUrl = "https://zentryc.com/downloads/zenplus-agent",
  [switch]$RequireSigning,
  [switch]$AllowUnsignedDevelopmentBuild,
  # Some managed build workstations prohibit executing newly compiled,
  # unsigned binaries. This may be used only for unsigned validation builds;
  # production-signed builds must always execute the embedded-payload verifier.
  [switch]$SkipPayloadExecutionVerification,
  # Endpoint-protection on some build hosts blocks freshly compiled test
  # binaries; run "go test ./..." manually when skipping here.
  [switch]$SkipTests,
  [switch]$SkipLanguagePacks,
  # Reuse the checked-in Windows resource objects on application-controlled
  # build hosts where `go run` cannot execute the temporary rsrc binary.
  [switch]$UseExistingResources
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path "$PSScriptRoot\.."
if (-not $TimestampUrl) {
  $TimestampUrl = "http://timestamp.digicert.com"
}
$SigningThumbprint = ($SigningThumbprint -replace '\s', '').ToUpperInvariant()
if ($RequireSigning -and $AllowUnsignedDevelopmentBuild) {
  throw "-RequireSigning and -AllowUnsignedDevelopmentBuild cannot be used together"
}
if ($SkipPayloadExecutionVerification -and -not $AllowUnsignedDevelopmentBuild) {
  throw "-SkipPayloadExecutionVerification is restricted to unsigned development builds"
}
if ($SkipLanguagePacks) {
  throw "Installer builds always require the complete offline APM bundle. Run prepare-apm-bundle.ps1 -SkipLanguagePacks directly for gateway-only development work."
}
if (-not $SigningThumbprint -and -not $AllowUnsignedDevelopmentBuild) {
  throw "Production builds require -SigningThumbprint or ZENPLUS_SIGNING_THUMBPRINT. Use -AllowUnsignedDevelopmentBuild only for local test artifacts."
}
$portableGo = Join-Path $root ".tools\go\bin\go.exe"
if (Test-Path $portableGo) {
  $go = $portableGo
} else {
  $goCommand = Get-Command go.exe -ErrorAction SilentlyContinue
  if ($null -eq $goCommand) {
    $goCommand = Get-Command go -ErrorAction SilentlyContinue
  }
  if ($null -eq $goCommand) {
    throw "Go was not found in .tools\go or PATH"
  }
  $go = $goCommand.Path
}

# Some endpoint-protection policies block test binaries under %TEMP%.
$env:GOTMPDIR = Join-Path $root ".gotmp"
New-Item -ItemType Directory -Force -Path $env:GOTMPDIR | Out-Null

$modelSource = Get-Content (Join-Path $root "internal\model\model.go") -Raw
if ($modelSource -notmatch 'AgentVersion\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"') {
  throw "Unable to read AgentVersion from internal\model\model.go"
}
$Version = $Matches[1]

$embedFlags = ""
if ($ControllerUrl) {
  $embedFlags += " -X zenplus-agent/internal/config.embeddedControllerURL=$ControllerUrl"
}
$normalizedPublicBaseUrl = $PublicBaseUrl.TrimEnd('/')
if (-not $normalizedPublicBaseUrl.StartsWith('https://')) {
  throw "-PublicBaseUrl must use HTTPS"
}
$embedFlags += " -X zenplus-agent/internal/selfupdate.publicUpdateBaseURL=$normalizedPublicBaseUrl"

function Invoke-Go {
  & $go @args
  if ($LASTEXITCODE -ne 0) {
    throw "go $($args -join ' ') failed with exit code $LASTEXITCODE"
  }
}

function Invoke-Rsrc {
  Invoke-Go run github.com/akavel/rsrc@v0.10.2 @args
}

function Update-ApmBundleManifest {
  param([string]$Stage)
  $manifestPath = Join-Path $Stage "bundle-manifest.json"
  $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  $stagePrefix = [IO.Path]::GetFullPath($Stage).TrimEnd('\') + '\'
  $files = Get-ChildItem -Path $Stage -Recurse -File |
    Where-Object { $_.FullName -ne $manifestPath } |
    Sort-Object FullName |
    ForEach-Object {
      $fullName = [IO.Path]::GetFullPath($_.FullName)
      if (-not $fullName.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "APM bundle file escaped the staging directory: $fullName"
      }
      [ordered]@{
        path = $fullName.Substring($stagePrefix.Length).Replace("\", "/")
        size = [int64]$_.Length
        sha256 = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLowerInvariant()
      }
    }
  $gateway = Join-Path $Stage "gateway\zenplus-telemetry-gateway.exe"
  foreach ($component in $manifest.components) {
    if ($component.name -eq "zenplus-telemetry-gateway") {
      $component.sha256 = (Get-FileHash -Algorithm SHA256 $gateway).Hash.ToLowerInvariant()
    }
  }
  $manifest.generated_at = (Get-Date).ToUniversalTime().ToString("o")
  $manifest.files = @($files)
  $json = $manifest | ConvertTo-Json -Depth 8
  [IO.File]::WriteAllText($manifestPath, $json, (New-Object Text.UTF8Encoding($false)))
}

function Assert-CompleteApmBundle {
  param([string]$Stage)
  $manifestPath = Join-Path $Stage "bundle-manifest.json"
  if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "The APM bundle manifest is missing: $manifestPath"
  }
  $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  $requiredComponents = @(
    "zenplus-telemetry-gateway",
    "opentelemetry-dotnet-auto",
    "opentelemetry-javaagent",
    "opentelemetry-node-auto",
    "opentelemetry-python-auto"
  )
  $componentNames = @($manifest.components | ForEach-Object { $_.name })
  foreach ($component in $requiredComponents) {
    if ($componentNames -notcontains $component) {
      throw "The APM bundle is incomplete: component '$component' is missing"
    }
  }
  $requiredFiles = @(
    "gateway\zenplus-telemetry-gateway.exe",
    "instrumentation\dotnet\net\OpenTelemetry.AutoInstrumentation.StartupHook.dll",
    "instrumentation\dotnet\win-x64\OpenTelemetry.AutoInstrumentation.Native.dll",
    "instrumentation\java\opentelemetry-javaagent.jar",
    "instrumentation\node\bootstrap.js",
    "instrumentation\node\node_modules\@opentelemetry\auto-instrumentations-node\package.json",
    "instrumentation\python\wheelhouse\opentelemetry_distro-0.65b0-py3-none-any.whl",
    "instrumentation\python\wheelhouse\opentelemetry_instrumentation_flask-0.65b0-py3-none-any.whl",
    "instrumentation\python\wheelhouse\opentelemetry_instrumentation_requests-0.65b0-py3-none-any.whl",
    "instrumentation\python\Install-ZenPlusPythonTracing.ps1",
    "instrumentation\python\README.txt",
    "instrumentation\python\constraints.txt"
  )
  foreach ($relativePath in $requiredFiles) {
    $path = Join-Path $Stage $relativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
      throw "The APM bundle is incomplete: required file '$relativePath' is missing"
    }
  }
}

function Find-SignTool {
  $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($null -ne $command) {
    return $command.Path
  }
  $kits = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
  if (Test-Path $kits) {
    $candidate = Get-ChildItem -Path (Join-Path $kits "*\x64\signtool.exe") -ErrorAction SilentlyContinue |
      Sort-Object FullName -Descending |
      Select-Object -First 1
    if ($null -ne $candidate) {
      return $candidate.FullName
    }
  }
  throw "signtool.exe was not found in PATH or the Windows 10 SDK"
}

function Invoke-SignArtifacts {
  param([string[]]$Paths)
  if (-not $SigningThumbprint) {
    return
  }
  $currentUserCert = "Cert:\CurrentUser\My\$SigningThumbprint"
  $localMachineCert = "Cert:\LocalMachine\My\$SigningThumbprint"
  $useMachineStore = $false
  if (Test-Path $currentUserCert) {
    $certificate = Get-Item $currentUserCert
  } elseif (Test-Path $localMachineCert) {
    $certificate = Get-Item $localMachineCert
    $useMachineStore = $true
  } else {
    throw "code-signing certificate $SigningThumbprint was not found in CurrentUser or LocalMachine My store"
  }
  if (-not $certificate.HasPrivateKey) {
    throw "code-signing certificate $SigningThumbprint has no accessible private key"
  }
  if ($certificate.NotAfter -le (Get-Date)) {
    throw "code-signing certificate $SigningThumbprint expired at $($certificate.NotAfter.ToString('o'))"
  }
  $signTool = Find-SignTool
  foreach ($path in $Paths) {
    $resolved = (Resolve-Path $path).Path
    $signArgs = @("sign", "/sha1", $SigningThumbprint, "/fd", "SHA256", "/tr", $TimestampUrl, "/td", "SHA256")
    if ($useMachineStore) {
      $signArgs += "/sm"
    }
    $signArgs += $resolved
    & $signTool @signArgs
    if ($LASTEXITCODE -ne 0) {
      throw "signtool sign failed for $resolved with exit code $LASTEXITCODE"
    }
    & $signTool verify /pa /all $resolved
    if ($LASTEXITCODE -ne 0) {
      throw "signtool verification failed for $resolved with exit code $LASTEXITCODE"
    }
  }
}

function Ensure-Wix {
  $wixDir = Join-Path $root ".tools\wix314"
  $candle = Join-Path $wixDir "candle.exe"
  $light = Join-Path $wixDir "light.exe"
  if ((Test-Path $candle) -and (Test-Path $light)) {
    return @{ Candle = $candle; Light = $light }
  }

  New-Item -ItemType Directory -Force -Path $wixDir | Out-Null
  $zip = Join-Path $root ".tools\wix314-binaries.zip"
  $url = "https://github.com/wixtoolset/wix3/releases/download/wix314rtm/wix314-binaries.zip"
  try {
    Invoke-WebRequest -Uri $url -OutFile $zip
    Expand-Archive -Force -Path $zip -DestinationPath $wixDir
  }
  finally {
    Remove-Item -Force $zip -ErrorAction SilentlyContinue
  }
  if (!(Test-Path $candle) -or !(Test-Path $light)) {
    throw "WiX tools were not found after extracting $zip"
  }
  return @{ Candle = $candle; Light = $light }
}

function Invoke-Wix {
  param(
    [string]$Candle,
    [string]$Light
  )
  $wixObj = "dist\ZenPlusAgent.wixobj"
  & $Candle `
    -nologo `
    -dProductVersion="$Version" `
    -dSetupExe="$root\dist\ZenPlusAgentSetup-x64.exe" `
    -dAssetsDir="$root\assets" `
    -out $wixObj `
    "packaging\ZenPlusAgent.wxs"
  if ($LASTEXITCODE -ne 0) {
    throw "candle.exe failed with exit code $LASTEXITCODE"
  }
  # Named zenplus-agent-<version>.msi to match the controller's publish
  # scanner (/opt/zenplus/artifacts/agents/windows/).
  & $Light `
    -nologo `
    -ext WixUIExtension `
    -ext WixUtilExtension `
    -sice:ICE61 `
    -sice:ICE71 `
    -out "dist\zenplus-agent-$Version.msi" `
    $wixObj
  if ($LASTEXITCODE -ne 0) {
    throw "light.exe failed with exit code $LASTEXITCODE"
  }
}

Push-Location $root
try {
  New-Item -ItemType Directory -Force -Path "dist" | Out-Null
  Invoke-Go mod tidy
  if (-not $SkipTests) {
    Invoke-Go test ./...
  }
  & (Join-Path $root "scripts\prepare-apm-bundle.ps1")
  if ($LASTEXITCODE -ne 0) {
    throw "APM bundle preparation failed with exit code $LASTEXITCODE"
  }
  $apmStage = Join-Path $root ".tools\apm\stage"
  $apmGateway = Join-Path $apmStage "gateway\zenplus-telemetry-gateway.exe"
  Invoke-SignArtifacts @($apmGateway)
  Update-ApmBundleManifest -Stage $apmStage
  Assert-CompleteApmBundle -Stage $apmStage
  $apmBundleManifest = Get-Content -LiteralPath (Join-Path $apmStage "bundle-manifest.json") -Raw | ConvertFrom-Json
  $apmGatewayComponents = @($apmBundleManifest.components | Where-Object { $_.name -eq "zenplus-telemetry-gateway" })
  if ($apmGatewayComponents.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$apmGatewayComponents[0].version)) {
    throw "The APM bundle manifest must contain exactly one versioned zenplus-telemetry-gateway component"
  }
  $apmGatewayVersion = [string]$apmGatewayComponents[0].version
  if ($UseExistingResources) {
    foreach ($resource in @("cmd\zenplus-agent-app\rsrc.syso", "cmd\zenplus-agent-installer\rsrc.syso")) {
      if (-not (Test-Path $resource)) {
        throw "-UseExistingResources requires checked-in resource object $resource"
      }
    }
  } else {
    Invoke-Rsrc -arch amd64 -ico "assets\zenplus-agent.ico" -manifest "cmd\zenplus-agent-app\zenplus-agent-app.manifest" -o "cmd\zenplus-agent-app\rsrc.syso"
    Invoke-Rsrc -arch amd64 -ico "assets\zenplus-agent.ico" -manifest "cmd\zenplus-agent-installer\zenplus-agent-installer.manifest" -o "cmd\zenplus-agent-installer\rsrc.syso"
  }
  Invoke-Go build -trimpath -ldflags="-s -w$embedFlags" -o "dist\zenplus-agent.exe" ".\cmd\zenplus-agent"
  Invoke-Go build -trimpath -ldflags="-s -w$embedFlags" -o "dist\zenplus-agentctl.exe" ".\cmd\zenplus-agentctl"
  Invoke-Go build -trimpath -ldflags="-s -w -H=windowsgui$embedFlags" -o "dist\zenplus-agent-app.exe" ".\cmd\zenplus-agent-app"
  Invoke-Go build -trimpath -ldflags="-s -w -H=windowsgui$embedFlags" -o "dist\zenplus-agent-user.exe" ".\cmd\zenplus-agent-user"
  Invoke-SignArtifacts @(
    "dist\zenplus-agent.exe",
    "dist\zenplus-agentctl.exe",
    "dist\zenplus-agent-app.exe",
    "dist\zenplus-agent-user.exe"
  )
  $payloadDir = "cmd\zenplus-agent-installer\payload"
  New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null
  Copy-Item -Force "dist\zenplus-agent.exe" (Join-Path $payloadDir "zenplus-agent.exe")
  Copy-Item -Force "dist\zenplus-agentctl.exe" (Join-Path $payloadDir "zenplus-agentctl.exe")
  Copy-Item -Force "dist\zenplus-agent-app.exe" (Join-Path $payloadDir "zenplus-agent-app.exe")
  Copy-Item -Force "dist\zenplus-agent-user.exe" (Join-Path $payloadDir "zenplus-agent-user.exe")
  if (-not (Test-Path (Join-Path $apmStage "gateway\zenplus-telemetry-gateway.exe"))) {
    throw "The managed APM gateway is missing from $apmStage"
  }
  Copy-Item -Recurse -Force $apmStage (Join-Path $payloadDir "apm")
  try {
    Invoke-Go build -trimpath -tags installerpayload -ldflags="-s -w -H=windowsgui$embedFlags" -o "dist\ZenPlusAgentSetup-x64.exe" ".\cmd\zenplus-agent-installer"
    # Sign before executing the verifier so production builds also work on
    # Windows hosts that enforce Smart App Control or WDAC.
    Invoke-SignArtifacts @("dist\ZenPlusAgentSetup-x64.exe")
    if (-not $SkipPayloadExecutionVerification) {
      $payloadVerification = Start-Process `
        -FilePath (Join-Path $root "dist\ZenPlusAgentSetup-x64.exe") `
        -ArgumentList @("/verify-payload", "/quiet") `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
      if ($payloadVerification.ExitCode -ne 0) {
        throw "Embedded installer payload verification failed with exit code $($payloadVerification.ExitCode)"
      }
    }
  }
  finally {
    Remove-Item -Recurse -Force $payloadDir -ErrorAction SilentlyContinue
  }
  $wix = Ensure-Wix
  Invoke-Wix -Candle $wix.Candle -Light $wix.Light
  $msiPath = Join-Path $root "dist\zenplus-agent-$Version.msi"
  Invoke-SignArtifacts @($msiPath)

  # The standalone setup is the canonical Windows package. It owns safe
  # in-place upgrades itself and is also the modern interactive wizard.
  # The MSI remains an optional enterprise wrapper, but is not published as
  # the primary update artifact.
  $setupReleasePath = Join-Path $root "dist\zenplus-agent-$Version.exe"
  Copy-Item -Force "dist\ZenPlusAgentSetup-x64.exe" $setupReleasePath
  $msi = Get-Item -LiteralPath $msiPath
  $setup = Get-Item -LiteralPath $setupReleasePath
  $msiHash = (Get-FileHash -Algorithm SHA256 $msi.FullName).Hash.ToLowerInvariant()
  $setupHash = (Get-FileHash -Algorithm SHA256 $setup.FullName).Hash.ToLowerInvariant()
  $msiSignature = Get-AuthenticodeSignature $msi.FullName
  $setupSignature = Get-AuthenticodeSignature $setup.FullName
  $releasedAt = (Get-Date).ToUniversalTime().ToString("o")
  $manifest = [ordered]@{
    latest_version = $Version
    version = $Version
    platform = "windows"
    arch = "amd64"
    channel = "stable"
    file_name = $setup.Name
    file_size = [int64]$setup.Length
    size_bytes = [int64]$setup.Length
    sha256 = $setupHash
    download_url = "$normalizedPublicBaseUrl/stable/$($setup.Name)"
    release_notes_url = "$normalizedPublicBaseUrl/"
    signature_status = $setupSignature.Status.ToString()
    signing_subject = if ($null -ne $setupSignature.SignerCertificate) { $setupSignature.SignerCertificate.Subject } else { $null }
    requires_authenticode = $true
    install_profiles = @("infrastructure", "apm", "combined")
    apm_profile_available = $true
    apm_gateway_version = $apmGatewayVersion
    setup_file_name = $setup.Name
    setup_size_bytes = [int64]$setup.Length
    setup_sha256 = $setupHash
    setup_download_url = "$normalizedPublicBaseUrl/stable/$($setup.Name)"
    setup_signature_status = $setupSignature.Status.ToString()
    msi_file_name = $msi.Name
    msi_size_bytes = [int64]$msi.Length
    msi_sha256 = $msiHash
    msi_signature_status = $msiSignature.Status.ToString()
    released_at = $releasedAt
    built_at_utc = $releasedAt
  }
  $manifestJson = $manifest | ConvertTo-Json
  $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  [System.IO.File]::WriteAllText((Join-Path $root "dist\agent-manifest.json"), $manifestJson, $utf8NoBom)
  Get-ChildItem "dist\*.exe","dist\*.msi" | ForEach-Object {
    $hash = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLowerInvariant()
    "$($_.Name)  $hash"
  }
}
finally {
  Pop-Location
}

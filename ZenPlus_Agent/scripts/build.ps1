param(
  # The controller address may be compiled into a release for convenience.
  # Enrollment credentials are never compiled into or patched into artifacts.
  [string]$ControllerUrl = $env:ZENPLUS_EMBED_CONTROLLER_URL,
  [string]$SigningThumbprint = $env:ZENPLUS_SIGNING_THUMBPRINT,
  [string]$TimestampUrl = $env:ZENPLUS_TIMESTAMP_URL,
  [switch]$RequireSigning,
  # Endpoint-protection on some build hosts blocks freshly compiled test
  # binaries; run "go test ./..." manually when skipping here.
  [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path "$PSScriptRoot\.."
if (-not $TimestampUrl) {
  $TimestampUrl = "http://timestamp.digicert.com"
}
$SigningThumbprint = ($SigningThumbprint -replace '\s', '').ToUpperInvariant()
if ($RequireSigning -and -not $SigningThumbprint) {
  throw "-RequireSigning was set but no -SigningThumbprint or ZENPLUS_SIGNING_THUMBPRINT was provided"
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

function Invoke-Go {
  & $go @args
  if ($LASTEXITCODE -ne 0) {
    throw "go $($args -join ' ') failed with exit code $LASTEXITCODE"
  }
}

function Invoke-Rsrc {
  Invoke-Go run github.com/akavel/rsrc@latest @args
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
    -out $wixObj `
    "packaging\ZenPlusAgent.wxs"
  if ($LASTEXITCODE -ne 0) {
    throw "candle.exe failed with exit code $LASTEXITCODE"
  }
  # Named zenplus-agent-<version>.msi to match the controller's publish
  # scanner (/opt/zenplus/artifacts/agents/windows/).
  & $Light `
    -nologo `
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
  Invoke-Rsrc -arch amd64 -ico "assets\zenplus-agent.ico" -manifest "cmd\zenplus-agent-app\zenplus-agent-app.manifest" -o "cmd\zenplus-agent-app\rsrc.syso"
  Invoke-Rsrc -arch amd64 -ico "assets\zenplus-agent.ico" -manifest "cmd\zenplus-agent-installer\zenplus-agent-installer.manifest" -o "cmd\zenplus-agent-installer\rsrc.syso"
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
  try {
    Invoke-Go build -trimpath -tags installerpayload -ldflags="-s -w -H=windowsgui$embedFlags" -o "dist\ZenPlusAgentSetup-x64.exe" ".\cmd\zenplus-agent-installer"
  }
  finally {
    Remove-Item -Recurse -Force $payloadDir -ErrorAction SilentlyContinue
  }
  Invoke-SignArtifacts @("dist\ZenPlusAgentSetup-x64.exe")
  $wix = Ensure-Wix
  Invoke-Wix -Candle $wix.Candle -Light $wix.Light
  $msiPath = Join-Path $root "dist\zenplus-agent-$Version.msi"
  Invoke-SignArtifacts @($msiPath)
  $msi = Get-Item -LiteralPath $msiPath
  $msiHash = (Get-FileHash -Algorithm SHA256 $msi.FullName).Hash.ToLowerInvariant()
  $msiSignature = Get-AuthenticodeSignature $msi.FullName
  $manifest = [ordered]@{
    latest_version = $Version
    version = $Version
    platform = "windows"
    arch = "amd64"
    channel = "stable"
    file_name = $msi.Name
    size_bytes = [int64]$msi.Length
    sha256 = $msiHash
    signature_status = $msiSignature.Status.ToString()
    signing_subject = if ($null -ne $msiSignature.SignerCertificate) { $msiSignature.SignerCertificate.Subject } else { $null }
    built_at_utc = (Get-Date).ToUniversalTime().ToString("o")
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

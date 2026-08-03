param(
  # Baked into every binary so the published MSI installs and enrolls with
  # zero operator input. Override per-build; both stay changeable later via
  # agent.yaml or MSI properties (CONTROLLER_URL / ENROLLMENT_TOKEN).
  [string]$ControllerUrl = $env:ZENPLUS_EMBED_CONTROLLER_URL,
  [string]$EnrollmentToken = $env:ZENPLUS_EMBED_ENROLLMENT_TOKEN,
  # Endpoint-protection on some build hosts blocks freshly compiled test
  # binaries; run "go test ./..." manually when skipping here.
  [switch]$SkipTests
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path "$PSScriptRoot\.."
$go = Join-Path $root ".tools\go\bin\go.exe"
if (!(Test-Path $go)) {
  throw "Portable Go toolchain was not found at $go"
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
if ($EnrollmentToken) {
  $embedFlags += " -X zenplus-agent/internal/config.embeddedEnrollmentToken=$EnrollmentToken"
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
  $wix = Ensure-Wix
  Invoke-Wix -Candle $wix.Candle -Light $wix.Light
  Get-ChildItem "dist\*.exe","dist\*.msi" | ForEach-Object {
    $hash = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLowerInvariant()
    "$($_.Name)  $hash"
  }
}
finally {
  Pop-Location
}

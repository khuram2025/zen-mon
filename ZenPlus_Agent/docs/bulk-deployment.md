# Deploying ZenPlus Agent at scale (Windows)

Setup runs unattended from any deployment tool: Group Policy, Microsoft
Intune, Configuration Manager (MECM/SCCM), PDQ Deploy, or a remote PowerShell
loop. No enrollment secret is placed on the endpoint or the command line.
Each installation registers as **Pending authorization**, and an operator
approves all of them at once in Agent Fleet.

## 1. Pick the package

| Servers need | Package | Size | `INSTALL_PROFILE` |
| --- | --- | --- | --- |
| Server monitoring | `zenplus-agent-core-<version>.exe` / `.msi` | ~21 MB | `infrastructure` |
| Server monitoring and APM | `zenplus-agent-<version>.exe` / `.msi` | ~87 MB | `combined` |
| APM only | `zenplus-agent-<version>.exe` / `.msi` | ~87 MB | `apm` |

Download it from the appliance with Agent Fleet → **Download agent**, or from
`https://<appliance>/api/v1/agents/packages/windows/latest?flavor=core` (or
`flavor=full`). The appliance does not need internet access. Packages are
signed by **Zentryc LLC** (Microsoft Artifact Signing, timestamped).

A server can be switched between profiles later from Agent Fleet (**Change
monitoring profile**), so start with server monitoring if unsure.

## 2. Command line

Setup is a Windows (GUI-subsystem) program. A Command Prompt or PowerShell does
not wait for it: typed on its own, the command returns at once with no output
while Setup runs, or fails, in the background. At a prompt, wait for it and
read the exit code. Deployment tools (Intune, Configuration Manager, PDQ) wait
on their own and take the plain command.

PowerShell (elevated):

```powershell
$p = Start-Process .\zenplus-agent-core-1.14.2.exe -Wait -PassThru -ArgumentList '/machine','/quiet','/norestart','CONTROLLER_URL=https://192.168.8.221','INSTALL_PROFILE=infrastructure','/log','C:\Windows\Temp\ZenPlusAgent-install.log'
"Exit code: $($p.ExitCode)"
```

Command Prompt (elevated), EXE:

```cmd
start "" /wait zenplus-agent-core-1.14.2.exe /machine /quiet /norestart CONTROLLER_URL=https://192.168.8.221 INSTALL_PROFILE=infrastructure /log "C:\Windows\Temp\ZenPlusAgent-install.log"
echo Exit code %ERRORLEVEL%
```

Command Prompt (elevated), MSI:

```cmd
start "" /wait msiexec /i zenplus-agent-core-1.14.2.msi /qn /norestart CONTROLLER_URL=https://192.168.8.221 INSTALL_PROFILE=infrastructure /L*v "C:\Windows\Temp\ZenPlusAgent-install.log"
echo Exit code %ERRORLEVEL%
```

As the install command of a deployment tool, use the same line without
`start "" /wait`.

From agent 1.14.3, a silent install started from a Command Prompt or PowerShell
also prints its result in that window (for example "setup failed (exit code
740): ... requires an administrator").


| Switch or property | Meaning |
| --- | --- |
| `/machine` (`/allusers`) | All-users installation with the Windows service. Use it for servers. |
| `/quiet` (`/silent`, `/verysilent`, `/S`, `/qn`) | No UI. |
| `/norestart` | Accepted for compatibility. Setup never restarts Windows. |
| `CONTROLLER_URL=` (`/controller-url`) | Appliance address. Required on first install; an upgrade keeps the saved one unless given. |
| `INSTALL_PROFILE=` (`/profile`) | `infrastructure`, `combined` or `apm`. First install defaults to `infrastructure`; an upgrade keeps the installed profile. |
| `CONTROLLER_CA_FILE=` | PEM bundle to trust an appliance certificate issued by a private CA. |
| `/log <path>` | Append Setup's result to this file. Its folder must exist. |
| `/no-start-menu` | Skip the Start Menu shortcut. |

Switches are case-insensitive. Unknown switches are rejected (exit code 1639)
rather than ignored. The server-monitoring package refuses `combined` and
`apm`, and refuses to replace an installation whose applications are
instrumented by APM.

Uninstall:

```cmd
"%ProgramFiles%\ZenPlus\Agent\ZenPlusAgentSetup.exe" /uninstall /quiet
```

Add `/purge` to also delete `%ProgramData%\ZenPlus\Agent` (queued data,
identity and logs). Uninstall restores applications ZenPlus instrumented.

## 3. Exit codes

Setup uses Windows Installer codes, which deployment tools already
understand:

| Code | Meaning | Action |
| --- | --- | --- |
| 0 | Installed, upgraded or removed | — |
| 1618 | Another ZenPlus installation is running (for example a self-update) | Retry later; tools do this automatically |
| 1639 | Invalid switch, property or profile for this package | Fix the command line |
| 740 | Not elevated | Run as SYSTEM or an administrator |
| 1603 | Installation failed and was rolled back | Read the `/log` file |

A failed installation or upgrade is rolled back: the previous version, its
configuration and the Windows service are restored.

## 4. Tool recipes

**Group Policy.** Use a computer startup script (runs as SYSTEM) that installs
from a share when the agent is missing:

```powershell
$key = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\ZenPlus Agent'
if (-not (Test-Path $key)) {
  $p = Start-Process '\\fileserver\software\zenplus-agent-core-1.14.2.exe' -Wait -PassThru -ArgumentList @(
    '/machine', '/quiet', '/norestart',
    'CONTROLLER_URL=https://192.168.8.221', 'INSTALL_PROFILE=infrastructure',
    '/log', 'C:\Windows\Temp\ZenPlusAgent-install.log')
  exit $p.ExitCode
}
```

Or assign the MSI with Software Installation. Set its properties with a
transform, or use the startup script above.

**Intune (Win32 app).** Wrap the EXE with the Win32 Content Prep Tool.
- Install command: the EXE line above.
- Uninstall command: the uninstall line above.
- Install behavior: System.
- Detection rule: registry, `HKLM\Software\Microsoft\Windows\CurrentVersion\Uninstall\ZenPlus Agent`,
  value `DisplayVersion`, string comparison, equals `1.14.2`.
- Return codes: the defaults already treat 0 as Success and 1618 as Retry.
  Other codes are failures.

**Configuration Manager.** Create an application with a Script Installer
deployment type.
- Commands: the install and uninstall lines above.
- Detection: the same registry value.
- Installation behavior: Install for system.
- Return codes: 1618 is already *Fast retry* by default.

**PDQ Deploy / remote PowerShell.**

```powershell
$servers = Get-Content .\servers.txt
Invoke-Command -ComputerName $servers -ThrottleLimit 20 -ScriptBlock {
  $p = Start-Process '\\fileserver\software\zenplus-agent-core-1.14.2.exe' -Wait -PassThru -ArgumentList @(
    '/machine', '/quiet', '/norestart', 'CONTROLLER_URL=https://192.168.8.221',
    'INSTALL_PROFILE=infrastructure', '/log', 'C:\Windows\Temp\ZenPlusAgent-install.log')
  [pscustomobject]@{ Server = $env:COMPUTERNAME; ExitCode = $p.ExitCode }
}
```

Without a file share, each server can pull the package straight from the
appliance. The script checks the SHA-256 and the Zentryc LLC signature, and
exits with Setup's code:

```powershell
$s = Join-Path $env:TEMP 'zenplus-agent-install.ps1'
Invoke-WebRequest -UseBasicParsing -Uri 'https://192.168.8.221/api/v1/agents/install.ps1' -OutFile $s
& $s -ControllerUrl 'https://192.168.8.221' -InstallProfile infrastructure
```

## 5. Approve, then manage centrally

1. In Agent Fleet, filter **Pending authorization**, select all, and choose
   **Authorize**. Each agent receives its own credential, protected with
   Windows DPAPI. Until approval, agents collect and queue data locally.
2. From then on, manage the fleet from the appliance:
   - **Trigger upgrade** installs a published version, with progress shown
     for each agent.
   - **Change monitoring profile** adds APM (installs the APM package) or
     removes it (installs the server-monitoring package). Removal first
     restores instrumented applications, but only when you tick
     *Also remove APM instrumentation*, because those applications restart.
   - **Change policy** and **Change update ring** apply collection settings
     and rollout rings.

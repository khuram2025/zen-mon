# ZenPlus Agent 1.14.3

Windows x64. Authenticode publisher: **Zentryc LLC** (Microsoft Artifact
Signing, timestamped). Includes every 1.14.2 change.

## New Setup wizard

Double-clicking Setup opens a standard Windows installation wizard with
Back, Next and Cancel buttons:

1. **Welcome.** Shows the version and package. If ZenPlus Agent is already
   installed, it says that Setup will upgrade (or repair) it and keep its
   settings, identity and approval.
2. **Terms of installation.** The installation and data-collection terms, with
   *I accept* / *I do not accept*. Next stays disabled until they are
   accepted.
3. **Installation options.**
   - Choose All users (recommended; the Windows service) or Only for me
     (evaluation).
   - The full package also offers Server monitoring and APM, or APM only.
     The server-monitoring package no longer shows a monitoring-profile
     section.
   - Choose whether to create a Start Menu shortcut.
4. **Connect to ZenPlus.** Enter the appliance address. A Test connection
   button checks the appliance is reachable before installing.
5. **Ready to install.** A summary of the choices. The Install button carries
   the Windows administrator shield when approval will be needed.
6. **Installing.** Progress, with details.
7. **Finished.**
   - On success: *Launch ZenPlus Agent*, and a reminder that the computer
     appears in Agent Fleet as Pending authorization.
   - On failure: the reason in plain words, with the details below.

Cancel asks for confirmation. The window has a fixed wizard size.

## Administrator approval for standard users

- **Approval request.** Setup now requests elevation with the wizard as the
  prompt's owner window and COM initialised, as Windows requires. A standard
  user therefore gets the Windows credential prompt in front of Setup.
  Previously the request could fail without showing any prompt.
- **Up-front check.** Before Install, Setup checks whether this account can
  be elevated at all. If User Account Control is off, or set to deny requests
  from standard users, the options page says so and asks the user to sign in
  as, or ask, an administrator instead of failing later.
- **Cancelled approval.** Setup shows "Setup was cancelled"; nothing is
  installed.
- **Failure reasons.** The elevated Setup writes a log. If it fails, the
  wizard shows its actual reason (for example an unreachable or invalid
  appliance address), not just an exit code.

## Silent installs from a prompt

- **Result in the window.** Setup is a GUI program, so a Command Prompt or
  PowerShell returns without waiting for it. From 1.14.3, a silent install
  started from a console also prints its result in that console, for example
  `ZenPlus Agent setup failed (exit code 740): silent machine installation
  requires an administrator...` or `ZenPlus Agent 1.14.3 setup completed
  successfully.` Deployment tools, the MSI and self-updates are unaffected.
- **Waiting and exit codes.** For scripts, keep using `start "" /wait` or
  `Start-Process -Wait` to wait for Setup and read its exit code.

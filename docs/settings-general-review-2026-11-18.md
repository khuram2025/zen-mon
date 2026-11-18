# Settings → General: review of every tab (2026-11-18)

Scope: `/settings/general`, all 13 tabs, dashboard and API. Each finding was
confirmed by reading both sides; those marked **Fixed** have a regression test
(`server/tests/test_general_settings_review.py`,
`server/tests/test_directory_user_role.py`, the dashboard contract scripts).
**Open** means it needs a decision, a design or a migration of its own and was
not built unasked; each has a recommendation.

Tabs found: Company, Monitoring, SMTP / Email, Appearance, Users & Access
(Users, Roles & Permissions, Authentication), Licenses, Updates, Storage,
Security & TLS, Sensors, MIB Library, Support, My Profile.

## The reported bug

**A directory user's role could not be changed. Fixed.** See
`docs/directory-account-roles.md` for the rule. In short: the directory decides
whether the account may sign in; the role mapping decides its role unless an
administrator set one by hand, and a hand-set role survives sign-in. What the
mapping gives is recorded and shown beside it, never dropped. Migration 195.

## Tab strip

| Finding | State |
|---|---|
| Monitoring was a tab but not in the sidebar or the page catalog, so a role limited to selected pages could never be given it | **Fixed** |
| Support was shown to every account, but every support route is administrator-only: non-admins saw a form that could only fail | **Fixed**: the tab needs `system.admin` |
| Old `/settings/<tab>` addresses (security, storage, smtp, profile, appearance, …) all opened Company | **Fixed** |
| Tab labels differed from the sidebar ("Security" / "Security & TLS", "Profile" / "My Profile") | **Fixed** |

## Company

| Finding | State |
|---|---|
| Tab shown to `settings.manage`, loaded with `settings.view`: a role with manage only got an empty form whose Save would blank the real settings | **Fixed**: either permission loads; a failed load shows an error and no form |
| "Unsaved changes" stayed forever after a save the server normalised (trailing `/` on the appliance URL, surrounding spaces) | **Fixed** |
| Time zone picker listed only the first 400 zones | **Fixed** |
| Appliance URL is ignored when `APP_BASE_URL` is set in the environment and the page does not say so | **Open**: show the effective value read-only when the environment overrides it |

## Monitoring

| Finding | State |
|---|---|
| An invalid value inside the collapsed "Advanced" section disabled Save with nothing highlighted on screen | **Fixed**: the section opens and is marked when it holds an error |
| SSH terminal docstring says administrators only; the gate is `settings.manage` | **Open** (documentation only) |

## SMTP / Email

| Finding | State |
|---|---|
| The tab showed the legacy settings key, not the default gateway that actually sends mail. After a change on the Gateways page, any Save here put the old server and password back and renamed the gateway | **Fixed**: the tab reads and writes the gateway in use and keeps its name |
| The stored password was returned to the browser in clear text | **Fixed**: never returned; blank keeps it, an explicit tick removes it |
| "Send test" tested the saved values, not what was typed | **Fixed** |
| A failed load showed defaults that Save would store (email off, empty host) | **Fixed** |
| No server validation: any port, any encryption word, "enabled" with no host | **Fixed** |
| The Gateways page (`/gateways`, outside this page) still returns gateway secrets to the browser | **Open**: apply the same keep-on-blank masking there; it changes that page's form contract |
| No TLS certificate-verification option, no Reply-To | **Open** (feature) |

## Appearance

No bugs. Theme and density are stored in the browser only, so they do not
follow the user to another computer. **Open**: store them per user on the
server; add "follow the system theme".

## Users & Access → Users

| Finding | State |
|---|---|
| Role of a directory user could not be changed (the reported bug) | **Fixed** |
| Sign-in matched usernames by exact case. A case variant of a disabled or tag-scoped directory account ("JSmith" for "jsmith") was provisioned as a second, active, unscoped account; a local "Admin" typing "admin" was refused | **Fixed**: sign-in ignores case; unique index on `lower(username)` (skipped with a notice where variants already exist) |
| An administrator could remove their own MFA from the users list with no password or code, bypassing the "MFA required" policy | **Fixed** |
| "Effective access" and the access-review CSV listed permissions a page-limited role does not hold | **Fixed** |
| Full name and email of a directory user are editable but replaced at the next sign-in, with no hint | **Fixed** (the form says so) |
| Deleting the last row of the last page left an empty table with no pager | **Fixed** |
| "New user" started on Viewer even when a delegated manager may not assign it (blank field, then 403) | **Fixed** |
| `PUT /users/{id}/contact-methods` is open to `alerts.manage` / `users.manage` for any account, including administrators above the caller | **Open, by design today** (on-call needs it); recommend limiting a delegated manager to accounts they may manage |
| Bulk actions, CSV import, account expiry, auto-disable of unused accounts, cleanup of accounts deleted from the directory, converting a local account to a directory one | **Open** (features) |

## Users & Access → Roles & Permissions

| Finding | State |
|---|---|
| Role history showed an empty entry for a revision that changed only the scope, page limit, name or description | **Fixed** |
| Edit is offered for a role above the manager's own access and fails only on save | **Open**: disable it from `/roles/assignable` data |

## Users & Access → Authentication

| Finding | State |
|---|---|
| The privileged-mapping confirmation compared role names only: with one rule already mapping to Administrator, "everyone else becomes Administrator" (default role) or a second group saved unconfirmed | **Fixed**: every new (who → privileged role) grant is confirmed |
| Disabling a source that every active administrator signs in through locked everyone out | **Fixed**: refused with an explanation until a local administrator exists |
| Provider test ignored "remove the stored secret" and tested with the secret about to be removed | **Fixed** |
| LDAP test showed a green "Connection" step when the server was unreachable | **Fixed** |
| Sources table always showed "—" for local accounts | **Fixed** |
| The last *local* administrator can still be deleted or disabled while a directory administrator exists | **Open**: decide whether one local break-glass administrator must always remain |
| Changing the server address in the dialog and pressing Test sends the stored bind password to the new address | **Open**: require re-entering the secret when the server changes |
| Stale-save protection reads the live revision instead of the one the dialog opened with | **Open** (rare; capture the revision on open) |
| Session lifetime is fixed at 60 minutes; no idle timeout, password expiry or history setting; no lockout or MFA for directory accounts | **Open** (features; session settings are the most asked-for) |

## Licenses

| Finding | State |
|---|---|
| A 401 / 403 from the licensing portal was relayed as ours, which signs the administrator out instead of showing the reason | **Fixed** (502 with the portal's message) |
| One unreadable field in the cached subscription broke Licenses, Updates and the header bell (500) | **Fixed** |
| Enter pressed twice during registration sent two registrations | **Fixed** in the form; **Open**: a server-side lock |
| Portal 404 on refresh reports "Subscription refreshed"; no deregister flow; no expiry banner | **Open** |

## Updates

| Finding | State |
|---|---|
| Saving any setting restarted the updater timer, which **starts a timer that was stopped on purpose**. On .221 the timer is stopped and auto-update is on, so saving the maintenance window there would have started update checks | **Fixed**: systemd is touched only when the interval changes, and the helper uses `try-restart` |
| Check interval had no bounds (0 or negative written to `agent.conf`) | **Fixed** |
| The maintenance window is only tested when the timer fires: a one-hour window with a 4 h or 24 h interval is rarely hit and nothing warns | **Open**: validate the window against the interval, or schedule a check at window start |
| No signed offline package upload, no channel selector, no start/stop control for the timer | **Open** (features) |

## Storage

| Finding | State |
|---|---|
| Disk actions (add, claim, grow) timed out in the browser after 30 s while the server kept working for up to 30 minutes | **Fixed** |
| Backup download and import timed out after 30 s | **Fixed** (browser side) |
| nginx's `/api/` location sets no body-size limit on the installer path, so a backup upload over 1 MB is refused (413) | **Open**: needs an installer / helper change and a streamed, ticketed download instead of an in-memory blob |
| Clearing a retention field did not restore anything, while the page said "blank uses the built-in retention" | **Wording fixed**; restoring the shipped per-table TTLs is **Open** (they differ per table, so one number per category could shorten history) |
| A retention save fails entirely while ClickHouse is down | **Open**: save the policy first, report TTL errors separately |
| "Keep last N" scheduled backups keeps N + 1 | **Open** |

## Security & TLS

| Finding | State |
|---|---|
| After enabling HTTPS the redirect dropped `?tab=security` | **Fixed** |
| With HSTS on, "Generate & install" a self-signed certificate or switching HTTPS off locks browsers out for up to six months, with no warning | **Open**: confirm before replacing a live certificate, refuse while HSTS is applied, serve `max-age=0` first |
| CSR "Discard" deletes the pending private key without confirmation; the access-policy Save is disabled without explanation when its helper is missing | **Open** |
| No certificate-expiry alert; no timed auto-revert for access or TLS changes; no list of trusted SSH host keys | **Open** (features) |

## Sensors

| Finding | State |
|---|---|
| The assignment picker asked for 500 service checks; the API allows 200, so the list was always empty. Devices beyond the first 200 could not be assigned | **Fixed**: both lists are read in full |
| Deleting a sensor (or saving its assignments) can leave "sensor only" devices monitored by nobody, and the confirmation does not say so | **Open**: return those targets to the controller and name the count in the confirmation; touches the monitoring-policy model |
| Duplicate site name returns 500; Disable has no confirmation; sensor name / tags and site details cannot be edited; key rotation always answers 409 | **Open** |

## MIB Library

| Finding | State |
|---|---|
| Tab shown to `settings.manage`; list, upload, delete and compile wanted device permissions, so the tab was unusable for that role and Delete failed silently | **Fixed** |
| After a failed upload the same file could not be retried | **Fixed** |
| Re-uploading a MIB under another extension orphans the old file; no multi-file upload; no audit entry | **Open** |

## Support

Tab gating fixed (above). Bundle download, redaction and path handling were
checked and are sound. **Open**: a case reference field.

## My Profile

| Finding | State |
|---|---|
| A mistyped code when turning off two-step verification or renewing recovery codes signed the user out (the API answered 401) | **Fixed** (400, stays signed in) |
| "Your time zone" is stored but nothing uses it; the hint claims it shows on-call shifts in that zone | **Open**: use it in on-call views and notices, or remove the field |
| Local users cannot edit their own name or email; no "sign out my other sessions" | **Open** (features) |

## Verified on the appliance (192.168.8.221, read-only)

Through the owner's signed-in browser session, GET requests only: version
1.25.16; 13 tabs; three local users and **no directory account** (LDAP has one
mapping but is disabled), so the reported bug could not be reproduced there
without changing the appliance and was reproduced and fixed in tests instead;
the sidebar has no Monitoring entry; the updater timer is stopped with
auto-update on. Nothing was written to the appliance.

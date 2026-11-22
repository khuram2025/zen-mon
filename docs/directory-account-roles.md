# Roles of directory accounts (LDAP / Active Directory, RADIUS, OIDC)

Released with migration 195. Code: `server/app/services/role_source.py`,
`server/app/api/v1/auth.py` (sign-in), `server/app/api/v1/users.py` (user API),
`dashboard/src/components/forms/UserFormDialog.tsx` (Edit user).

## What was wrong

An account that signs in through a directory could not have its role changed
in Settings → Users & Access. The Edit dialog disabled the Role field, the user
API refused the change (`409`, "change the mapping instead"), and every sign-in
overwrote `users.role` with whatever the provider's role mapping gave. The only
way to give one person a different role was a new directory group and a new
mapping rule.

## The rule

LDAP / Active Directory defaults to **Only allow listed accounts** for new
provider configurations. In this mode, add the directory sign-in name under
**Settings → Users & Access → Users → New user**, select **LDAP / Active
Directory**, and assign a role. The user signs in with their directory password.
ZenPlus reads the configured name and email attributes when the account is
added and refreshes them after the user authenticates. An account can sign in
without an email address. If AD has no usable address, ZenPlus leaves it blank.
ZenPlus requires an active LDAP account in Users, and a role set there by an
administrator is sufficient after the directory verifies the password. Accounts
set to follow the directory continue to require a matching role mapping.

For established LDAP configurations with automatic creation enabled, turn on
**Only allow listed accounts** under the provider’s Role mapping tab to require
an existing account. Review any accounts previously created on first sign-in.

For accounts that still require a provider role mapping:

> The directory decides **whether** a directory account may sign in.
> The role mapping decides **which role** it holds, unless an administrator set
> one by hand. A hand-set role stays until an administrator hands the account
> back to the directory.

Neither side is dropped:

| | Follows the mapping (`role_source = directory`) | Set by hand (`role_source = manual`) |
|---|---|---|
| Role at sign-in | the mapped role is applied | the hand-set role is kept |
| What the mapping gives | applied, and recorded in `users.directory_role` | recorded in `users.directory_role`, shown beside the role |
| Directory maps no role (and no default role) | sign-in refused | sign-in refused |
| Directory rejects the password, or the source is disabled | sign-in refused | sign-in refused |
| Mapping points at a role that no longer exists | sign-in refused | signs in on the hand-set role |
| Account disabled in ZenPlus | sign-in refused | sign-in refused |

So removing someone from the directory group that admits them still locks them
out. What a hand-set role changes is only that a later change of group no
longer changes their role: that is the trade-off, and the page says so on the
account (users list, account details, Edit dialog, the provider's test dialog
and the access-review CSV all show "set by hand" with the directory's role).

## Using it

Edit user → **Role is decided by**:

* **LDAP / RADIUS / SSO role mapping** (default): the Role field shows the
  mapped role and is read-only.
* **An administrator (set here)**: choose the role. It is kept at every sign-in.

Switching back to the mapping applies the role the directory gave at the last
sign-in at once. When that is not known yet (the account has not signed in
since the upgrade) the account is signed out and takes the mapped role at its
next sign-in.

API: `PUT /users/{id}` accepts `role` for a directory account (implies
`role_source: "manual"`) and `role_source: "manual" | "directory"`. `GET`
returns `role_source` (`local` / `directory` / `manual`) and `directory_role`.

## Who may do it

The same delegation rules as for a local account: the actor needs
`users.manage`, must out-rank the account, and may only assign a role within
their own access. Handing an account back to the directory is checked against
the role the directory gives; when that is unknown, only a full administrator
may do it (only a full administrator controls the mapping that decides it).
Nobody changes who decides their own role.

## Other fields of a directory account

* **Visibility scope** and **Active / Disabled** are set by hand and are never
  touched by sign-in.
* **Full name and email** for LDAP come from the configured directory attributes
  when the account is added and when it signs in. OIDC updates fields supplied by
  the provider at sign-in. RADIUS keeps manually entered profile fields.
* **Password, forced password change, MFA** are the directory's: the actions
  are hidden and the API refuses them.
* **Username**: sign-in matches ignoring case and the database enforces
  uniqueness ignoring case, so `JSmith` is the account `jsmith`, not a second one.

### Username rules in LDAP listed-accounts mode

Choose **AD username** in Role mapping, enter the AD account name (for example
`a.test`), and select a role. AD must verify the password and return that
sAMAccountName. Username comparison ignores case. An exact username rule is
evaluated before group rules. A user first admitted by a username rule must
continue matching one in listed-accounts mode, including after the first login
creates their profile. Removing the rule denies subsequent sign-ins. Existing
group entries retain their group semantics. An account explicitly assigned in
Users keeps its assigned role.

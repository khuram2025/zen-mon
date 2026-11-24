# ZenPlus 1.25.28

Settings → General → Security & TLS now includes **Dashboard session timeout**. The appliance keeps its configured fixed sign-in duration until an administrator chooses a different policy. An administrator can select a timeout after inactivity or no automatic timeout for the appliance or an individual account.

For a NOC display, choose its account under **Per-user override**, select **No automatic timeout**, and save. Changing an effective policy signs the affected sessions out once. Sign in again to use the new policy. Explicit sign-out, disabling the ZenPlus account and session revocation still end access.

Interactive dashboard input extends an idle session. Background polls and dashboard refreshes leave the idle deadline in place. An unavailable TLS status no longer hides the independent security controls. A delayed unauthorized response from an older session no longer signs out the current login.

The appliance-only package includes PostgreSQL migration 214. Existing sessions keep their original expiry. The required update checkpoint remains 1.24.36.

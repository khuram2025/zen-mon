# ZenPlus 1.24.38

Require the cumulative 1.24.36 bridge before this and future appliance releases.
That bridge includes the signed Windows agent 1.14.4 core/full EXE and MSI
installers, so an appliance upgrading from 1.24.29 receives the current local
agent package catalog before the smaller appliance-only update.

The normal OTA path is 1.24.29 -> 1.24.36 -> 1.24.38. Appliances already on
1.24.36 or 1.24.37 can upgrade directly to 1.24.38. Intermediate application
versions are cumulative and need no additional hop. Endpoint agent deployment
continues through the existing controlled agent upgrade workflow.

The signed minimum and portal minimum must both be 1.24.36. The build policy
defaults to this floor and rejects explicit lower minimums for future releases.
The OTA planner offers the required bridge first; each hop must complete before
the next offer. Missing, ineligible or repeatedly failed bridges block the path.
The appliance also checks the signed prerequisite before applying a package.
The portal promotion guard must also be deployed from this release: for
1.24.38 and later it rejects lower minimums and verifies the catalog bridge's
signed package, age, publication and rollout coverage before promotion.

Includes all 1.24.37 search, column sorting and stable selection improvements.
This is an appliance-only package that preserves existing Windows installers.
Previously signed packages and their prerequisites remain unchanged.

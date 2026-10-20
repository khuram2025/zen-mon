# ZenPlus 1.24.22 — Live subscription node count

Settings > General > Licenses: the Subscription card's **Subscription nodes** now uses the same calculation as Node capacity, from this appliance's live devices + servers plus the other appliances' share from the last check-in. It used to show zentryc.com's figure from the last check-in, which could lag behind adds and deletes or still carry the old devices + service checks count. **Refresh** still pushes the current count to zentryc.com.

No schema change.

# ZenPlus 1.24.36

Alert rule editing now waits for an explicit Save or Create action on Review.
Previously, moving from Schedule to Review could turn the clicked Next button
into a submit button, save the rule and close the wizard. Navigation and save
controls now have separate identities, navigation cancels default submission,
and implicit or duplicate form submissions are rejected. Opening another rule
resets the wizard before painting its prior state.

Reset settings now distinguish automatic resolution from closing notifications.
Restart and shutdown notices close after their event observation window;
operators can enable an end-of-window notification without the payload silently
discarding that choice. The closing message does not claim that a server has
recovered or powered off. New lifecycle rules keep silent closing as the default.

Changing a percentage metric preserves valid zero thresholds, zero hold times
and equality operators. Server message previews explicitly show their built-in,
read-only wording. Other rules retain custom template editing and reset controls.
Closing/recovery previews remain available while notifications are off, and Send
test uses the selected trigger or recovery preview, including unsaved wording.

Includes the existing signed Windows agent 1.14.4 core/full EXE and MSI packages.
This release does not force upgrades or restarts of monitored Windows hosts.

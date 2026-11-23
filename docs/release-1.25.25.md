# ZenPlus 1.25.25

Services list: the status cards tell the truth, the 30-day bars load quickly,
and a failing monitor says how long and why.

- The Up, Down, Warning and Unknown cards count only monitors that are
  switched on. A paused monitor keeps its last result, so a page with 20
  paused monitors last seen Down no longer shows "Down 22 - needs attention
  now". Paused monitors stay in their own card. Clicking a status card lists
  only monitors that are switched on.
- The 30-day bars appear in well under a second after the first load of the
  day: finished days are kept and only today is read on each refresh (before,
  every refresh took about 4 seconds, even with three monitors).
- A failing monitor reads "Down for 2h 14m - checked 13s ago" with the reason
  underneath ("HTTP 503 Service Unavailable", "Connection refused"). A monitor
  that is down shows no response time instead of "0 ms".
- Search runs once typing pauses (Enter searches at once), and the bars have
  a colour key.

Note: a result that arrives late for an earlier day, or a maintenance window
edited after the fact, can take up to 10 minutes to show on that day's bar.

No migrations. Appliance-only package; minimum upgrade base 1.24.36; signed
Windows agent catalog preserved. Details: PR #193.

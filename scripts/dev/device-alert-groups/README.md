# Device-page alert grouping vectors

`vectors.json` holds the grouping key and label rules for repeated alerts on the device page (field review D-09) to one
behaviour in both places that apply them: `app.services.device_insights` (summary API: issues bar and Summary "Recent
events") and `dashboard/src/pages/device/eventsModel.ts` (Events tab). `server/tests/test_device_alert_groups.py` and
`dashboard/scripts/test-device-events.mjs` replay every case.

The file is hand-written. When a rule changes, change both implementations and the cases in the same change. See
`docs/device-detail-page.md` ("Repeated alerts").

* `key_cases[]`: `{name, rule_id, rule_name, metadata, expect}`. The group key of an alert with these fields must be
  `expect` (`null`: never grouped).
* `label_cases[]`: `{name, titles, rule_name, metadata, expect}`. `titles` are the members' titles, newest first; the
  label (without the count) must be `expect`.

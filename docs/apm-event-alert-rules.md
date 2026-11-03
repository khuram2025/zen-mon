# Synthetic and SLO Alert Rules

In Alert Rules, choose **APM & RUM**, then **Synthetic Scenario Down**.
The default **All scenarios** scope includes current and future enabled scenarios.
A specific scenario can also be selected. Choose channels, severity/priority,
optional recovery messages, schedule and escalation routing in the usual wizard.

Confirmation belongs to the scenario: the first attempt plus its configured
retries must all fail. A success during retry prevents an outage notification.
Each rule/scenario pair has one incident, so another scenario can alert even
while an earlier incident is open. Repeated failures update that incident's
failed step and error; a successful run resolves it. Notifications include the
scenario name, failed step and error, with the configured regional timestamp.
Manual and scheduled runs share a cross-worker lock, including retries and
delivery, to prevent overlapping runs from creating duplicate incidents.

**SLO Burn** adds equivalent All SLOs or specific-SLO routing. It uses the existing
multi-window burn evaluator and combines simultaneously breaching tiers into
one incident per rule/SLO. Recovery requires complete, non-breaching window
readings; missing traffic does not resolve an incident.

For both event triggers, an enabled matching Alert Rule owns notification
routing. Local scenario/SLO channel settings remain the fallback when no enabled
matching rule exists. This avoids sending both local and rule notifications.
Multiple deliberately overlapping Alert Rules each own their own incident.
These event triggers do not add another hold or numeric threshold; confirmation
is already defined on the scenario or by the SLO burn windows.

Rules use the common dispatcher, durable recipient delivery keys, schedule,
per-entity cooldown, priority, grouping and escalation policies. An unannounced
trigger does not send a recovery. Disabling a rule stops its notifications;
confirmed recovery can still close its existing incident.

The existing APM latency percentiles, error rate, throughput, Apdex and browser
RUM triggers remain available. Reserved anomaly/no-data metric names have no
active producer and are not offered as working triggers.

The scenario editor independently fetches and normalizes configured notification
channels, so opening Synthetics directly works like visiting Channels first.
Loading, failure/retry and empty-channel states are explicit.

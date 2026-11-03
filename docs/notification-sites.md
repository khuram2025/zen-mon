# Independent notification sites

Incident ownership stays in the originating site's database. Administrators can
select an ordered set of delivery sites for each notification channel and include
the origin's local sender. Each remote site uses its own provider credentials and
records a durable receipt without creating a duplicate local incident.

This capability is off by default. Apply migrations 173 and 174, commission an
independent site identity and local delivery channel, and explicitly set
`NOTIFICATION_SITE_ROUTING_ENABLED=true` for a qualified cell. Do not duplicate a
commissioned cell's identity by cloning its database into another independent
site. HA members of the same cell share that database identity.

## Administration

The Gateways page contains Notification sites when the feature is commissioned.
On the receiving site, authorize the origin UUID and map an origin channel UUID
to a local channel. Copy the one-time pairing credential to the origin's Add a
sender site form. Pairing verifies the sender UUID over HTTPS against the supplied
private CA or normal platform trust. Channel mappings constrain recipient scope;
an origin cannot provide an arbitrary SMTP host or arbitrary recipients.

At the origin, arrange the paired senders and This site in the desired order.
Changes require `settings.manage`; reads require settings access. Enable/disable
operations and pairing/policy changes are audited. A newly authorized event starts
at the preferred sender again. No persistent failback hold-down is implemented.

Provider credentials remain on each sending site. The outbound peer credential
is encrypted with the application's existing encryption mechanism. An inbound
peer stores a credential hash. Machine endpoints authenticate that scoped peer
credential independently of dashboard login. TLS is verified; redirects and
environment proxies are disabled for this protocol.

## Delivery and uncertainty

The origin obtains its normal incident/event/channel/recipient delivery key
before routing. The remote inbox uses a deterministic delivery UUID and an
immutable payload digest. Its row is committed before contacting the provider.
Concurrent requests, including an identical grant replay, cannot start two
provider calls. A proven non-delivery may be retried using a fresh grant;
accepted and uncertain receipts are not automatically resent.

A health probe contains no message. Failed probes and definitely-unsent provider
attempts can advance to the next selected sender. Provider acceptance is distinct
from inbox custody, and neither proves that a person received or read a message.

The default **Hold for review** policy stops automatic fallback if the remote
provider outcome is unknown. **Local safety** is an explicit availability-first
choice and requires This site last. Following an uncertain remote request, it
checks for a late acceptance receipt until the 60-second grant plus a 10-second
safety margin has elapsed. If acceptance remains unknown, it can send a labelled
local safety copy. Another remote sender is not tried during that ambiguous
handoff. A recovered accepted receipt avoids the local copy.

Local safety can produce a duplicate. The wait is not proof that a paused
remote process or external provider has cancelled its request. SMTP does not
provide exactly-once delivery. The label and durable attempt history preserve
this distinction instead of claiming a safe, invisible takeover.

## VMware evidence on 29 October 2026

Two independent three-node cells used isolated SMTP captures restricted to
`lab@acceptance.invalid`. The production primary at 192.168.8.221 was never a
fault target and retained its original API PID 3177.

| Scenario | Result |
|---|---|
| All site A VMs powered off | Site B detected and resolved an HTTP incident and sent one local trigger and one recovery; trigger at 31.28 seconds |
| B incident prefers A | Two messages accepted at A, none at B; trigger at 31.53 seconds |
| Preferred site A fully off | Two messages accepted at B, none at A; trigger at 39.39 seconds |
| A restored, new B incident | Two messages accepted at A, none at B; trigger at 27.59 seconds |
| WAN cut after remote SMTP capture, local safety selected | One remote acceptance and one explicitly labelled local copy; copy 69.24 seconds after remote capture |

The last scenario intentionally demonstrated duplicate risk: the receiver had
accepted the message while the origin could not observe its final response.
The measured interval begins at SMTP capture, after grant issuance. Lab routes,
frontend settings and captures were restored after the test.

## Remaining release qualification

The current remote inbox is request-driven: an interrupted processing attempt is
uncertain, not a promised asynchronous custody queue. Origin-owned durable work
remains responsible for new attempts. The protocol has no renewal endpoint or
provider-process deadline enforcement and must not be described as complete
execution fencing. The lab exercised SMTP; SMS and webhook providers need their
own runtime qualification. Direct-person notifications, reports, digests and
escalation crash boundaries require separate validation.

Credential rotation/expiry, receipt reconciliation tooling, bounded retention,
fleet configuration distribution, analytics replication, physical failure-domain
tests, clean-image acceptance and signing are still release gates. No customer
HA appliance is published by this implementation. See the approved design and
the updated VMware evidence document for the remaining phased work.

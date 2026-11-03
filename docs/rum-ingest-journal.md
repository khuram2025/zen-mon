# Durable browser RUM admission and rollup recovery

`RUM_INSERT_JOURNAL_ENABLED=1` is an opt-in commissioned HA mode. It requires
host journal recovery and controller admission on every API candidate. The
standalone default is unchanged. Origin restrictions, application binding,
privacy validation, quotas and event limits still apply before admission.

One immutable PostgreSQL block carries the exact raw rows and their derived
rollup operation. The recovery worker writes the raw block with its original
token, then always attempts the rollup with its own original token. Only both
successful responses complete the work record. Raw-row presence is never used
as evidence of rollup completion. A raw success followed by a rollup failure, a
lost response from either step, and a process or controller crash retain that
operation for recovery inside the qualified replay window.

The `rum_raw_rollup_v1` work kind has frozen columns and a fixed parameterized
rollup query in `rum_journal_contract_v1.py`. Incompatible changes require another
work kind and reader; preserve v1 while any corresponding work exists. This
avoids executing arbitrary SQL from a journal payload or changing the meaning
of an accepted operation during an upgrade.

Member identities retain the existing authenticated-key and logical event/view
identity. A regrouped retry joins its original block. PostgreSQL decides new
and duplicate admission atomically; this path does not trust the volatile Redis
reservation as an acknowledgement. A 202 means durable journal admission and
query visibility can lag. Response counts distinguish new admissions from
duplicates; a pending or held accepted member remains an existing admission.
Temporary failures return 503; invalid operations return 400. The SDK's memory
queue and retry limits still apply before durable admission.

The shared journal capacity, finite replay window and held-work rules apply.
Receipt retirement and operator reconciliation remain release gates. Do not
disable this writer, downgrade readers, or replace its contract while work is
pending or held. Upgrade every API candidate before commissioning the mode.

Commissioning against an existing legacy RUM dataset requires a separately
qualified cutover: preserve and reconcile legacy raw/rollup uncertainty and
producer retry identities before enabling the new receipts. The new admission
ledger cannot prove completion of earlier legacy work. Fresh role acceptance
does not establish that upgrade path. Sustained RUM capacity and complete
customer appliance release qualification remain separate requirements.

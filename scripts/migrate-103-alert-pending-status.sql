-- Migration 103: 'pending' alert status for min_duration holds
--
-- Alert rules have always stored a "Condition must exist for" hold time
-- (alert_rules.min_duration), and the periodic metric evaluators honour it as
-- a sustained window — but the status-transition engine (device up/down/
-- degraded, service check transitions) fired the instant the poller reported
-- the change. A 59-second degraded blip paged rules configured with a
-- 100-second hold.
--
-- The status engine is event-driven: it runs once, at the transition, so it
-- cannot wait the hold out in-process. Instead it now parks the alert row as
-- status='pending' with metadata.hold_until, and the hold sweeper
-- (app/services/alert_hold_service.py, 30s cadence alongside escalations)
-- settles the row when the hold expires:
--
--   still breaching  -> status='active', trigger notification dispatched
--                       (quiet hours and cooldown applied at that moment)
--   blip ended       -> status='resolved' with metadata.hold_cancelled=true,
--                       nothing ever dispatched
--
-- A recovery event arriving mid-hold resolves the pending row directly in the
-- engine. Pending rows carry metadata.notified=false, so the existing
-- recovery gating keeps their all-clear quiet, the escalation sweeper ignores
-- them, and they never start a flap cooldown.
--
-- This migration only widens the status CHECK constraint. The engine
-- feature-detects it (alert_engine._pending_supported) and keeps the old
-- fire-immediately behaviour until it has run, so deploy order is free.

ALTER TABLE alerts DROP CONSTRAINT IF EXISTS alerts_status_check;
ALTER TABLE alerts ADD CONSTRAINT alerts_status_check
    CHECK (status IN ('pending', 'active', 'acknowledged', 'resolved'));

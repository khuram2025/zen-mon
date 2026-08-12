package store

// NetPath persistence. These methods implement netpath.Store: loading probe
// definitions and writing each traceroute run (path dedup, snapshot, probe
// denormalized state, structural events) in a single transaction.

import (
	"context"
	"encoding/json"
	"fmt"
	"net"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/zenplus/poller/internal/checker/netpath"
)

// LoadNetpathProbes returns all enabled probe definitions.
func (s *PostgresStore) LoadNetpathProbes(ctx context.Context) ([]*netpath.Probe, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, name, target_host, host(target_ip)::text, target_port, protocol,
		       max_hops, probes_per_hop, flows, interval_s, run_now,
		       rtt_warn_ms, rtt_crit_ms, loss_warn_pct, loss_crit_pct,
		       last_path_hash, last_reached
		FROM netpath_probes
		WHERE enabled = TRUE
		ORDER BY name
	`)
	if err != nil {
		return nil, fmt.Errorf("query netpath probes: %w", err)
	}
	defer rows.Close()

	var out []*netpath.Probe
	for rows.Next() {
		var (
			p           netpath.Probe
			ipStr       *string
			port        *int
			lastHash    *int64
			lastReached *bool
		)
		if err := rows.Scan(
			&p.ID, &p.Name, &p.TargetHost, &ipStr, &port, &p.Protocol,
			&p.MaxHops, &p.ProbesPerHop, &p.Flows, &p.IntervalS, &p.RunNow,
			&p.RttWarnMs, &p.RttCritMs, &p.LossWarnPct, &p.LossCritPct,
			&lastHash, &lastReached,
		); err != nil {
			return nil, fmt.Errorf("scan netpath probe: %w", err)
		}
		if ipStr != nil && *ipStr != "" {
			p.TargetIP = net.ParseIP(*ipStr)
		}
		if port != nil {
			p.Port = *port
		}
		if lastHash != nil {
			p.LastPathHash = *lastHash
			p.HasLastHash = true
		}
		if lastReached != nil {
			p.LastReached = *lastReached
			p.HasLastReach = true
		}
		out = append(out, &p)
	}
	return out, rows.Err()
}

// ClearNetpathRunNow resets the on-demand run flag once a run has started.
func (s *PostgresStore) ClearNetpathRunNow(ctx context.Context, id uuid.UUID) error {
	_, err := s.pool.Exec(ctx, `UPDATE netpath_probes SET run_now = FALSE WHERE id = $1`, id)
	return err
}

// SaveNetpathRun persists one traceroute run atomically.
func (s *PostgresStore) SaveNetpathRun(ctx context.Context, in *netpath.SaveInput) error {
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin netpath tx: %w", err)
	}
	defer tx.Rollback(ctx)

	p := in.Probe
	r := in.Result

	// Authoritative previous state under row lock.
	var prevHash *int64
	var prevReached *bool
	if err := tx.QueryRow(ctx,
		`SELECT last_path_hash, last_reached FROM netpath_probes WHERE id = $1 FOR UPDATE`,
		p.ID).Scan(&prevHash, &prevReached); err != nil {
		return fmt.Errorf("lock netpath probe: %w", err)
	}

	// Path dedup — only when we actually saw hops.
	var pathID *uuid.UUID
	hopIPs := primaryPathIPs(r)
	if len(hopIPs) > 0 {
		var id uuid.UUID
		err := tx.QueryRow(ctx, `
			INSERT INTO netpath_paths (probe_id, path_hash, hop_count, hop_ips, first_seen, last_seen, seen_count)
			VALUES ($1, $2, $3, $4, NOW(), NOW(), 1)
			ON CONFLICT (probe_id, path_hash) DO UPDATE
			   SET last_seen = NOW(),
			       seen_count = netpath_paths.seen_count + 1,
			       hop_count = EXCLUDED.hop_count,
			       hop_ips = EXCLUDED.hop_ips
			RETURNING id
		`, p.ID, r.PathHash, r.HopCount, hopIPs).Scan(&id)
		if err != nil {
			return fmt.Errorf("upsert netpath path: %w", err)
		}
		pathID = &id
	}

	// path_changed: a different topology than the previous run (both had hops).
	pathChanged := false
	if prevHash != nil && len(hopIPs) > 0 && *prevHash != r.PathHash {
		pathChanged = true
	}

	// Snapshot insert.
	var snapID int64
	err = tx.QueryRow(ctx, `
		INSERT INTO netpath_snapshots
		    (probe_id, path_id, run_at, vantage, protocol, reached, path_changed,
		     hop_count, num_paths, rtt_ms, loss_pct, worst_hop_loss_pct, jitter_ms,
		     duration_ms, path_hash, status, error, hops, flows)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
		RETURNING id
	`,
		p.ID, pathID, in.RanAt, in.Vantage, p.Protocol, r.Reached, pathChanged,
		r.HopCount, r.NumPaths, nullFloat(r.RttMs, r.Reached), nullFloat(r.LossPct, true),
		nullFloat(r.WorstHopLoss, true), nullFloat(r.JitterMs, r.Reached),
		r.DurationMs, r.PathHash, in.Status, nullStr(r.Error),
		string(in.HopsJSON), string(in.FlowsJSON),
	).Scan(&snapID)
	if err != nil {
		return fmt.Errorf("insert netpath snapshot: %w", err)
	}

	// Probe denormalized state.
	_, err = tx.Exec(ctx, `
		UPDATE netpath_probes
		   SET last_run_at = $2, last_status = $3, last_rtt_ms = $4, last_loss_pct = $5,
		       last_hop_count = $6, last_num_paths = $7, last_path_hash = $8,
		       last_reached = $9, last_error = $10,
		       target_ip = COALESCE($11, target_ip), updated_at = NOW()
		 WHERE id = $1
	`,
		p.ID, in.RanAt, in.Status, nullFloat(r.RttMs, r.Reached), nullFloat(r.LossPct, true),
		r.HopCount, r.NumPaths, r.PathHash, r.Reached, nullStr(r.Error),
		ipText(in.ResolvedIP),
	)
	if err != nil {
		return fmt.Errorf("update netpath probe: %w", err)
	}

	// Structural events (path change + reachability transitions). First run
	// (prevReached NULL) emits nothing, to avoid noise.
	if pathChanged {
		det, _ := json.Marshal(map[string]any{
			"from_hash": *prevHash, "to_hash": r.PathHash,
			"hop_count": r.HopCount, "num_paths": r.NumPaths,
		})
		if err := insertNetpathEvent(ctx, tx, p.ID, "path_change", snapID, "warning", det); err != nil {
			return err
		}
	}
	if prevReached != nil {
		if *prevReached && !r.Reached {
			det, _ := json.Marshal(map[string]any{"last_hop_count": r.HopCount, "error": r.Error})
			if err := insertNetpathEvent(ctx, tx, p.ID, "unreachable", snapID, "critical", det); err != nil {
				return err
			}
		} else if !*prevReached && r.Reached {
			det, _ := json.Marshal(map[string]any{"rtt_ms": r.RttMs, "hop_count": r.HopCount})
			if err := insertNetpathEvent(ctx, tx, p.ID, "reachable", snapID, "info", det); err != nil {
				return err
			}
		}
	}

	// Register every distinct hop IP for the enrichment sweeper (fire-and-forget
	// within the tx; the ON CONFLICT keeps it cheap).
	if err := registerHopIPs(ctx, tx, r); err != nil {
		return err
	}

	return tx.Commit(ctx)
}

func insertNetpathEvent(ctx context.Context, tx pgx.Tx, probeID uuid.UUID, evType string, snapID int64, sev string, details []byte) error {
	_, err := tx.Exec(ctx, `
		INSERT INTO netpath_events (probe_id, event_type, snapshot_id, severity, details)
		VALUES ($1, $2, $3, $4, $5)
	`, probeID, evType, snapID, sev, string(details))
	if err != nil {
		return fmt.Errorf("insert netpath event %s: %w", evType, err)
	}
	return nil
}

// registerHopIPs inserts any newly seen hop IPs into netpath_hop_meta with a
// NULL enriched_at so the API sweeper picks them up for rDNS/ASN/device lookup.
func registerHopIPs(ctx context.Context, tx pgx.Tx, r *netpath.RunResult) error {
	seen := map[string]struct{}{}
	batch := &pgx.Batch{}
	for _, hop := range r.Hops {
		for _, n := range hop.Nodes {
			if n.IP == "" {
				continue
			}
			if _, ok := seen[n.IP]; ok {
				continue
			}
			seen[n.IP] = struct{}{}
			batch.Queue(`INSERT INTO netpath_hop_meta (ip) VALUES ($1::inet) ON CONFLICT (ip) DO NOTHING`, n.IP)
		}
	}
	if batch.Len() == 0 {
		return nil
	}
	br := tx.SendBatch(ctx, batch)
	defer br.Close()
	for i := 0; i < batch.Len(); i++ {
		if _, err := br.Exec(); err != nil {
			return fmt.Errorf("register hop ip: %w", err)
		}
	}
	return nil
}

// primaryPathIPs returns the ordered responder IPs of the first reached flow
// (or the longest observed flow when unreached) as the path's signature.
func primaryPathIPs(r *netpath.RunResult) []string {
	var best []string
	for _, f := range r.Flows {
		if f.Reached {
			cleaned := stripEmpty(f.Path)
			if len(cleaned) > 0 {
				return cleaned
			}
		}
		if c := stripEmpty(f.Path); len(c) > len(best) {
			best = c
		}
	}
	return best
}

func stripEmpty(in []string) []string {
	var out []string
	for _, s := range in {
		if s != "" {
			out = append(out, s)
		}
	}
	return out
}

func nullFloat(v float64, ok bool) *float64 {
	if !ok {
		return nil
	}
	return &v
}

func nullStr(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func ipText(ip net.IP) *string {
	if ip == nil {
		return nil
	}
	s := ip.String()
	return &s
}

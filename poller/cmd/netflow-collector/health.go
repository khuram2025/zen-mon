package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/netip"
	"time"

	"github.com/zenplus/poller/internal/netflow"
)

// Health thresholds.
const (
	// downAfter: inserts failing with no success for this long while data is
	// waiting means flows are not reaching the database.
	downAfter = 60 * time.Second
	// recentWindow: drops, rejections or kernel drops within this window keep
	// the status degraded.
	recentWindow = 5 * time.Minute
)

// healthDoc is the JSON served on NETFLOW_HEALTH_LISTEN /health. counters are
// cumulative since start (the same names as the flow_collector_stats columns,
// whose rows carry per-interval deltas); exporters break them down per
// exporter / observation domain / protocol.
type healthDoc struct {
	Status      string    `json:"status"` // ok | degraded | down
	Reasons     []string  `json:"reasons"`
	Service     string    `json:"service"`
	CollectorID string    `json:"collector_id"`
	BootID      string    `json:"boot_id"`
	StartedAt   time.Time `json:"started_at"`
	ServerTime  time.Time `json:"server_time"`
	Listen      string    `json:"listen"`
	Listening   bool      `json:"listening"`
	// sFlow listener (CR-03): the configured address ("" = disabled), whether
	// it runs, why not, and the interface counter samples it writes.
	SFlowListen    string         `json:"sflow_listen"`
	SFlowListening bool           `json:"sflow_listening"`
	SFlowError     string         `json:"sflow_error,omitempty"`
	IfCounters     ifCounterView  `json:"if_counters"`
	AppNames       appNamesHealth `json:"app_names"`
	ClockPolicy    string         `json:"clock_policy"`
	RcvbufBytes    int            `json:"rcvbuf_bytes"`
	// RcvbufRequestedBytes is NETFLOW_RCVBUF; RcvbufCapped says the kernel
	// granted less (net.core.rmem_max below it), so bursts the requested
	// buffer would absorb are dropped in the kernel.
	RcvbufRequestedBytes int              `json:"rcvbuf_requested_bytes"`
	RcvbufCapped         bool             `json:"rcvbuf_capped"`
	SocketDrops          *uint64          `json:"socket_drops"` // kernel drops on the flow sockets (NetFlow + sFlow) since they opened; null if unreadable
	Queue                queueHealth      `json:"queue"`
	Delivery             deliveryHealth   `json:"delivery"`
	Counters             counters         `json:"counters"` // process + every exporter
	Process              counters         `json:"process"`  // the exporter = '' row: insert/kernel/unattributed
	Exporters            []exporterHealth `json:"exporters"`
}

// appNamesHealth counts the NBAR2 / App-ID application names learned from
// exporter options data (CR-12) and those not yet written to flow_app_names.
type appNamesHealth struct {
	Known   int `json:"known"`
	Pending int `json:"pending"`
}

type queueHealth struct {
	QueueLen     int    `json:"queue_len"`
	QueueCap     int    `json:"queue_cap"`
	SpoolEnabled bool   `json:"spool_enabled"`
	SpoolRecords int    `json:"spool_records"`
	SpoolBytes   int64  `json:"spool_bytes"`
	SpoolDir     string `json:"spool_dir,omitempty"`
}

type deliveryHealth struct {
	LastInsertOKAt      *time.Time `json:"last_insert_ok_at"`
	LastInsertErrorAt   *time.Time `json:"last_insert_error_at"`
	LastInsertError     string     `json:"last_insert_error,omitempty"` // category only, never server text
	ConsecutiveFailures int        `json:"consecutive_failures"`
	FailingSince        *time.Time `json:"failing_since"` // first failure of the current streak; null while inserts succeed
	StatsWriteFailing   bool       `json:"stats_write_failing"`
	// HeldRecords are rows ClickHouse refused in a batch of their own; they
	// are retried once other rows are accepted (then written or dead-lettered).
	HeldRecords int `json:"held_records"`
}

func timePtr(t time.Time) *time.Time {
	if t.IsZero() {
		return nil
	}
	return &t
}

// health assembles the document and decides the status:
//   - down: not listening, or inserts have failed for downAfter with nothing
//     succeeding (flows are piling up or being dropped);
//   - degraded: inserts are being retried, data sits in the disk spool, or
//     records/datagrams were dropped or rejected recently;
//   - ok otherwise.
func (c *collector) health(now time.Time) healthDoc {
	totals, process, exporters := c.stats.snapshot()
	d := c.delivery.snapshot()
	qlen, qcap := c.queue.Len()
	doc := healthDoc{
		Service:              "zenplus-netflow-collector",
		CollectorID:          c.cfg.ID,
		BootID:               c.bootID,
		StartedAt:            c.startedAt,
		ServerTime:           now,
		Listen:               c.cfg.Listen,
		Listening:            c.listening.Load(),
		SFlowListen:          c.cfg.SFlowListen,
		SFlowListening:       c.sflowListening.Load(),
		SFlowError:           c.sflowErr,
		IfCounters:           c.ifCounters.view(),
		ClockPolicy:          string(c.cfg.ClockPolicy),
		RcvbufBytes:          c.rcvbuf,
		RcvbufRequestedBytes: c.cfg.RcvBuf,
		RcvbufCapped:         rcvbufCapped(c.cfg.RcvBuf, c.rcvbuf),
		Queue:                queueHealth{QueueLen: qlen, QueueCap: qcap, SpoolEnabled: c.spool != nil},
		Delivery: deliveryHealth{
			LastInsertOKAt:      timePtr(d.lastOK),
			LastInsertErrorAt:   timePtr(d.lastErr),
			LastInsertError:     d.lastErrLabel,
			ConsecutiveFailures: d.consecutiveFailures,
			FailingSince:        timePtr(d.failingSince),
			HeldRecords:         d.heldRecords,
		},
		Counters:  totals,
		Process:   process,
		Exporters: exporters,
		Reasons:   []string{},
	}
	if doc.Exporters == nil {
		doc.Exporters = []exporterHealth{}
	}
	doc.AppNames.Known, doc.AppNames.Pending = c.appNames.counts()
	active := map[string]map[netflow.DomainKey]int{
		netflow.ProtocolNetFlow9: c.v9Templates.ActiveTemplates(),
		netflow.ProtocolIPFIX:    c.ipfixTemplates.ActiveTemplates(),
	}
	for i := range doc.Exporters {
		e := &doc.Exporters[i]
		if addr, err := netip.ParseAddr(e.Exporter); err == nil {
			e.TemplatesActive = active[e.ExportProtocol][netflow.DomainKey{Exporter: addr, Domain: e.ObservationDomain}]
		}
	}
	if c.spool != nil {
		n, b := c.spool.usage()
		doc.Queue.SpoolRecords, doc.Queue.SpoolBytes, doc.Queue.SpoolDir = n, b, c.cfg.SpoolDir
	}

	c.kernelMu.Lock()
	var recentSocketDrops uint64
	if len(c.socketDropsAt) > 0 {
		v := c.socketDropsLast
		doc.SocketDrops = &v
		recentSocketDrops = v - c.socketDropsAt[0].drops
	}
	c.kernelMu.Unlock()
	c.statsMu.Lock()
	doc.Delivery.StatsWriteFailing = !c.statsFailedAt.IsZero()
	c.statsMu.Unlock()

	down := func(reason string) { doc.Status = "down"; doc.Reasons = append(doc.Reasons, reason) }
	degraded := func(reason string) {
		if doc.Status == "" {
			doc.Status = "degraded"
		}
		doc.Reasons = append(doc.Reasons, reason)
	}
	if !doc.Listening {
		down("not listening for flow datagrams")
	}
	if doc.SFlowListen != "" && doc.Listening && !doc.SFlowListening {
		degraded(fmt.Sprintf("sFlow listener %s is not running: %s", doc.SFlowListen, doc.SFlowError))
	}
	if d.consecutiveFailures > 0 {
		// The outage is measured from the first failure of the current streak:
		// a collector that was quiet for an hour (no insert, so no success
		// either) and then hits a 2-second ClickHouse blip is retrying, not
		// an hour into an outage.
		since := d.failingSince
		if since.IsZero() {
			since = d.lastErr
		}
		if since.IsZero() || since.After(now) {
			since = now
		}
		if failing := now.Sub(since); failing > downAfter {
			down(fmt.Sprintf("ClickHouse inserts failing for %s (%s)", failing.Round(time.Second), d.lastErrLabel))
		} else {
			degraded(fmt.Sprintf("retrying ClickHouse inserts (%s)", d.lastErrLabel))
		}
	}
	if doc.Queue.SpoolRecords > 0 {
		degraded(fmt.Sprintf("%d records waiting in the disk spool", doc.Queue.SpoolRecords))
	}
	if last := c.lastQueueDrop.Load(); last > 0 && now.Sub(time.Unix(0, last)) < recentWindow {
		degraded("records dropped because the queue was full")
	}
	if !d.lastDropAt.IsZero() && now.Sub(d.lastDropAt) < recentWindow {
		degraded("records lost at shutdown")
	}
	if !d.lastRejectAt.IsZero() && now.Sub(d.lastRejectAt) < recentWindow {
		degraded("records rejected by ClickHouse (dead-lettered)")
	}
	if recentSocketDrops > 0 {
		degraded(fmt.Sprintf("kernel dropped %d datagrams on the collector sockets in the last 5 minutes (receive buffer full)", recentSocketDrops))
	}
	if doc.RcvbufCapped {
		// A configuration issue, not a transient: the installer and updater
		// raise net.core.rmem_max (scripts/setup-netflow-sysctl.sh); a host
		// where that did not take loses flow bursts in the kernel.
		degraded(fmt.Sprintf("receive buffer smaller than requested: the kernel granted %s of the %s requested "+
			"(NETFLOW_RCVBUF); raise net.core.rmem_max to at least %d", sizeLabel(grantedRcvbuf(c.rcvbuf)),
			sizeLabel(c.cfg.RcvBuf), c.cfg.RcvBuf))
	}
	if d.heldRecords > 0 {
		degraded(fmt.Sprintf("%d records refused by ClickHouse held for another try", d.heldRecords))
	}
	if doc.Delivery.StatsWriteFailing {
		degraded("collector statistics cannot be written to ClickHouse")
	}
	if doc.IfCounters.WriteFailing {
		degraded("sFlow interface counters cannot be written to ClickHouse")
	}
	if doc.Status == "" {
		doc.Status = "ok"
	}
	return doc
}

// healthHandler serves the document; "down" answers 503 so a plain HTTP
// probe sees the failure without parsing the body.
func (c *collector) healthHandler(w http.ResponseWriter, _ *http.Request) {
	doc := c.health(time.Now().UTC())
	w.Header().Set("Content-Type", "application/json")
	if doc.Status == "down" {
		w.WriteHeader(http.StatusServiceUnavailable)
	}
	_ = json.NewEncoder(w).Encode(doc)
}

func (c *collector) runHealth(ctx context.Context) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", c.healthHandler)
	srv := &http.Server{Addr: c.cfg.HealthListen, Handler: mux, ReadHeaderTimeout: 3 * time.Second}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Printf("WARN: health server stopped: %v\n", err)
	}
}

// grantedRcvbuf is the usable receive buffer behind a SO_RCVBUF reading:
// Linux reports twice the size it applied (the other half is bookkeeping).
func grantedRcvbuf(reported int) int {
	return reported / 2
}

// rcvbufCapped reports whether the kernel granted less receive buffer than
// was requested (0 / unknown readings are never "capped").
func rcvbufCapped(requested, reported int) bool {
	return requested > 0 && reported > 0 && grantedRcvbuf(reported) < requested
}

func sizeLabel(n int) string {
	switch {
	case n >= 1<<20 && n%(1<<20) == 0:
		return fmt.Sprintf("%d MiB", n>>20)
	case n >= 1<<20:
		return fmt.Sprintf("%.1f MiB", float64(n)/(1<<20))
	case n >= 1<<10:
		return fmt.Sprintf("%d KiB", n>>10)
	}
	return fmt.Sprintf("%d bytes", n)
}

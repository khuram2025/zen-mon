package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/netip"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/ClickHouse/clickhouse-go/v2"
	"github.com/zenplus/poller/internal/netflow"
)

type clockPolicy string

const (
	clockClamp clockPolicy = "clamp" // implausible exporter times are placed at receipt time (default)
	clockTrust clockPolicy = "trust" // store exporter times as sent (lab replays of historical data)
)

type collectorConfig struct {
	ID               string
	Listen           string
	SFlowListen      string // sFlow v5 listener; empty = disabled
	HealthListen     string
	ClickHouseDSN    clickhouse.Options
	BatchSize        int
	FlushInterval    time.Duration
	AllowedExporters map[netip.Addr]bool // nil = allow all (BUG-07); non-nil, even empty, is enforced
	QueueMaxRecords  int                 // records held in memory before spilling/dropping the oldest (~480 B each)
	SpoolDir         string              // optional disk spool; empty = memory only
	SpoolMaxBytes    int64
	InsertTimeout    time.Duration
	ShutdownTimeout  time.Duration // how long a stop may spend delivering the queue
	StatsInterval    time.Duration
	RcvBuf           int
	ClockPolicy      clockPolicy
}

type collector struct {
	cfg            collectorConfig
	store          *chStore
	queue          *batchQueue
	spool          *diskSpool
	stats          *statsRegistry
	templates      *templateRegistry
	appNames       *appNameRegistry
	ifCounters     *ifCounterBuffer
	delivery       *deliveryState
	v9Templates    *netflow.V9TemplateCache
	ipfixTemplates *netflow.V9TemplateCache

	bootID         string
	startedAt      time.Time
	stopBy         atomic.Int64 // shutdown deadline (unix ns), 0 while running; caps every ClickHouse operation
	listening      atomic.Bool
	sflowListening atomic.Bool
	sflowErr       string // why the configured sFlow listener is not running (set before /health starts)
	rcvbuf         int
	sockInodes     []string // the flow sockets' /proc/net/udp inodes (kernel drop counters)

	lastParseLog       atomic.Int64
	lastImplausibleLog atomic.Int64
	lastReadLog        atomic.Int64
	lastStatsLog       atomic.Int64
	lastQueueDropLog   atomic.Int64
	lastRefusedLog     atomic.Int64
	lastQueueDrop      atomic.Int64 // unix nanos of the last record dropped from the queue
	templateLogs       logBudget

	kernelMu        sync.Mutex
	kernelBase      uint64 // host RcvbufErrors at the previous interval
	kernelBaseOK    bool
	socketDropsLast uint64
	socketDropsAt   []socketDropSample // recent samples for "dropped in the last 5 minutes"

	statsMu       sync.Mutex
	statsPending  []pendingStats // intervals whose insert failed, retried next time
	statsFailedAt time.Time

	// Template cache persistence (templatestate.go): the cache versions and
	// time of the last save. Used by the stats loop, then by finalFlush.
	tplSavedV9, tplSavedIPFIX uint64
	tplSavedAt                time.Time
	lastTemplateStateLog      atomic.Int64
}

// pendingStats is one interval's rows with the token they are always sent
// with, so a retry after an ambiguous failure cannot duplicate them.
type pendingStats struct {
	Token string
	Rows  []statsRow
}

// statsPendingFile keeps stats intervals that could not be written before a
// stop (ClickHouse down) so the next start writes them: the collector's own
// history then covers the outage it lived through.
const statsPendingFile = "stats-pending.json"

type socketDropSample struct {
	at    time.Time
	drops uint64
}

func main() {
	cfg := loadConfig()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	c, err := newCollector(cfg, time.Now().UTC())
	if err != nil {
		fatalf("netflow collector: %v", err)
	}

	// Listen first: ClickHouse being down or restarting must not make the
	// kernel refuse flows (X-14); they are queued/spooled until it is back.
	udp, err := listenUDP(cfg.Listen, cfg.RcvBuf)
	if err != nil {
		fatalf("netflow collector: listen %s: %v", cfg.Listen, err)
	}
	c.rcvbuf = effectiveRcvbuf(udp)
	if inode, ok := socketInode(udp); ok {
		c.sockInodes = append(c.sockInodes, inode)
	}
	c.listening.Store(true)
	// sFlow is optional: a listener that cannot start (port taken) is
	// reported in /health rather than stopping NetFlow/IPFIX collection.
	sfl := c.listenSFlow()
	// Baseline the kernel drop counters now, so the first interval reports
	// only drops that happened while this process was running.
	c.sampleKernel(time.Now().UTC())
	fmt.Printf("ZenPlus NetFlow collector %s listening on %s, sFlow %s (receive buffer %d bytes, clock policy %s, spool %s)\n",
		cfg.ID, cfg.Listen, sflowLabel(cfg.SFlowListen, sfl != nil), c.rcvbuf, cfg.ClockPolicy, spoolLabel(cfg.SpoolDir))

	bg, bgCancel := context.WithCancel(context.Background())
	var bgWG sync.WaitGroup
	bgWG.Add(4)
	go func() { defer bgWG.Done(); c.runHealth(bg) }()
	go func() { defer bgWG.Done(); c.runSealer(bg) }()
	go func() { defer bgWG.Done(); c.queue.runSpiller(bg) }()
	go func() { defer bgWG.Done(); c.runStatsLoop(bg) }()

	// The writer runs on its own context: it is only cut off when the
	// shutdown deadline expires, never by the signal itself (X-03).
	writerCtx, writerCancel := context.WithCancel(context.Background())
	writerDone := make(chan struct{})
	w := c.newWriter()
	go func() { defer close(writerDone); w.run(writerCtx) }()

	var readers sync.WaitGroup
	readers.Add(1)
	go func() { defer readers.Done(); c.runUDP(udp, c.handleDatagram) }()
	if sfl != nil {
		readers.Add(1)
		go func() { defer readers.Done(); c.runUDP(sfl, c.handleSFlowDatagram) }()
	}

	<-ctx.Done()
	fmt.Println("INFO: netflow collector stopping: draining queue")
	c.stopBy.Store(time.Now().Add(cfg.ShutdownTimeout).UnixNano())
	_ = udp.Close()
	if sfl != nil {
		_ = sfl.Close()
	}
	readers.Wait()
	c.listening.Store(false)
	c.sflowListening.Store(false)
	c.queue.Close()

	deadline := time.AfterFunc(cfg.ShutdownTimeout, writerCancel)
	<-writerDone
	deadline.Stop()
	writerCancel()

	bgCancel()
	bgWG.Wait()
	// The final statistics and registry writes get a budget of their own,
	// even when draining the queue used the whole shutdown deadline.
	c.stopBy.Store(time.Now().Add(finalFlushBudget).UnixNano())
	c.finalFlush()
	_ = c.store.conn.Close()
	totals, _, _ := c.stats.snapshot()
	fmt.Printf("INFO: netflow collector stopped: %d records written, %d dropped from the queue, %d spooled for the next start\n",
		totals.RecordsWritten, totals.RecordsDroppedQueue, spoolRecords(c.spool))
}

func newCollector(cfg collectorConfig, now time.Time) (*collector, error) {
	conn, err := openClickHouse(&cfg.ClickHouseDSN)
	if err != nil {
		return nil, fmt.Errorf("clickhouse options: %w", err)
	}
	c := &collector{
		cfg:            cfg,
		store:          &chStore{conn: conn, collectorID: cfg.ID},
		stats:          newStatsRegistry(now),
		templates:      newTemplateRegistry(),
		appNames:       newAppNameRegistry(),
		delivery:       &deliveryState{},
		v9Templates:    netflow.NewV9TemplateCache(),
		ipfixTemplates: netflow.NewV9TemplateCache(),
		bootID:         newBootID(),
		startedAt:      now,
		templateLogs:   logBudget{window: 10 * time.Second, limit: 100},
	}
	c.ifCounters = newIfCounterBuffer(cfg.ID + "-" + c.bootID)
	if cfg.SpoolDir != "" {
		sp, err := openDiskSpool(cfg.SpoolDir, cfg.SpoolMaxBytes)
		if err != nil {
			// Keep collecting with the memory queue rather than refusing flows.
			fmt.Printf("ERROR: netflow spool %s unavailable (%v); continuing with the memory queue only\n", cfg.SpoolDir, err)
		} else {
			c.spool = sp
			c.loadPendingStats()
			c.loadTemplateState()
			if n, b := sp.usage(); n > 0 {
				fmt.Printf("INFO: netflow spool holds %d records (%d bytes) from a previous run; replaying them first\n", n, b)
			}
		}
	}
	c.queue = newBatchQueue(cfg.BatchSize, cfg.QueueMaxRecords, cfg.FlushInterval, cfg.ID+"-"+c.bootID, c.spool)
	c.queue.onDrop = func(recs []netflow.Record) {
		c.stats.recordsDroppedQueue(recs)
		c.lastQueueDrop.Store(time.Now().UnixNano())
		c.logf(&c.lastQueueDropLog, "WARN: netflow queue full: dropped %d oldest records", len(recs))
	}
	c.queue.onDropped = func(n int) {
		c.stats.processDelta(func(k *counters) { k.RecordsDroppedQueue += uint64(n) })
		c.lastQueueDrop.Store(time.Now().UnixNano())
	}
	return c, nil
}

func (c *collector) newWriter() *flowWriter {
	dl := &deadLetter{}
	if c.cfg.SpoolDir != "" && c.spool != nil {
		dl.path, dl.maxBytes = filepath.Join(c.cfg.SpoolDir, "rejected.log"), 16<<20
	}
	return &flowWriter{
		store:         c.store,
		queue:         c.queue,
		stats:         c.stats,
		state:         c.delivery,
		insertTimeout: c.cfg.InsertTimeout,
		stopBy:        &c.stopBy,
		backoffMin:    500 * time.Millisecond,
		backoffMax:    30 * time.Second,
		deadLetter:    dl,
		now:           time.Now,
		sleep:         sleepCtx,
		heldRetest:    heldRetestEvery,
	}
}

func listenUDP(listen string, rcvbuf int) (*net.UDPConn, error) {
	addr, err := net.ResolveUDPAddr("udp", listen)
	if err != nil {
		return nil, err
	}
	udp, err := net.ListenUDP("udp", addr)
	if err != nil {
		return nil, err
	}
	if rcvbuf > 0 {
		// A bursty exporter (template refresh + a full cache flush) overruns
		// the default ~200 KB buffer; the kernel caps the request at
		// net.core.rmem_max, the effective size is logged and in /health.
		if err := udp.SetReadBuffer(rcvbuf); err != nil {
			fmt.Printf("WARN: netflow receive buffer %d bytes not applied: %v\n", rcvbuf, err)
		}
	}
	return udp, nil
}

// listenSFlow opens the sFlow socket when one is configured. It returns nil
// when sFlow is disabled or the socket could not be opened (recorded for
// /health).
func (c *collector) listenSFlow() *net.UDPConn {
	if c.cfg.SFlowListen == "" {
		return nil
	}
	conn, err := listenUDP(c.cfg.SFlowListen, c.cfg.RcvBuf)
	if err != nil {
		c.sflowErr = err.Error()
		fmt.Printf("ERROR: sFlow listener %s not started: %v (NetFlow/IPFIX collection continues)\n", c.cfg.SFlowListen, err)
		return nil
	}
	if inode, ok := socketInode(conn); ok {
		c.sockInodes = append(c.sockInodes, inode)
	}
	c.sflowListening.Store(true)
	return conn
}

func sflowLabel(listen string, up bool) string {
	switch {
	case listen == "":
		return "disabled"
	case !up:
		return listen + " (not started)"
	}
	return "on " + listen
}

// runSealer closes partial batches that have waited FlushInterval.
func (c *collector) runSealer(ctx context.Context) {
	tick := c.cfg.FlushInterval / 2
	if tick < 100*time.Millisecond {
		tick = 100 * time.Millisecond
	}
	t := time.NewTicker(tick)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			c.queue.SealDue()
		}
	}
}

// runStatsLoop writes flow_collector_stats rows at every interval boundary
// (aligned to wall-clock minutes for the default 60 s) and flushes the
// template and application-name registries and the sFlow interface counters.
func (c *collector) runStatsLoop(ctx context.Context) {
	interval := c.cfg.StatsInterval
	first := time.Until(time.Now().Truncate(interval).Add(interval))
	timer := time.NewTimer(first)
	defer timer.Stop()
	registry := time.NewTicker(10 * time.Second)
	defer registry.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-registry.C:
			c.flushRegistries(ctx)
			c.templateLogs.flush(time.Now())
		case <-timer.C:
			timer.Reset(time.Until(time.Now().Truncate(interval).Add(interval)))
			c.flushStats(ctx, time.Now().UTC())
			c.v9Templates.Prune()
			c.ipfixTemplates.Prune()
			c.saveTemplateState(time.Now(), false)
		}
	}
}

// flushRegistries writes what the decoders learned besides flows: template
// definitions, application names and sFlow interface counters. Each has its
// own insert timeout, so one slow write does not starve the others.
func (c *collector) flushRegistries(ctx context.Context) {
	run := func(write func(context.Context)) {
		ictx, cancel := opContext(ctx, c.cfg.InsertTimeout, &c.stopBy)
		defer cancel()
		write(ictx)
	}
	run(func(ictx context.Context) { c.templates.flush(ictx, c.insertTemplates) })
	run(func(ictx context.Context) { c.appNames.flush(ictx, time.Now().UTC(), c.insertAppNames) })
	run(func(ictx context.Context) { c.ifCounters.flush(ictx, time.Now().UTC(), c.insertIfCounters) })
}

func (c *collector) insertTemplates(ctx context.Context, rows []templateRow) error {
	if err := prepareSchema(ctx, c.store, c.delivery); err != nil {
		return err
	}
	err := c.store.InsertTemplates(ctx, rows)
	if err != nil && classify(err) == errSchema {
		c.delivery.schemaSuspect()
	}
	return err
}

func (c *collector) insertAppNames(ctx context.Context, rows []appNameRow) error {
	if err := prepareSchema(ctx, c.store, c.delivery); err != nil {
		return err
	}
	err := c.store.InsertAppNames(ctx, rows)
	if err != nil && classify(err) == errSchema {
		c.delivery.schemaSuspect()
	}
	return err
}

func (c *collector) insertIfCounters(ctx context.Context, token string, rows []ifCounterRow) error {
	if err := prepareSchema(ctx, c.store, c.delivery); err != nil {
		return err
	}
	err := c.store.InsertIfCounters(ctx, token, rows)
	if err != nil && classify(err) == errSchema {
		c.delivery.schemaSuspect()
	}
	return err
}

// flushStats closes the stats interval and writes its rows. Rows of intervals
// whose insert failed are kept (bounded to an hour) and retried with their
// original tokens, so an outage leaves no hole in the collector's own history.
func (c *collector) flushStats(ctx context.Context, now time.Time) {
	c.sampleKernel(now)
	qlen, qcap := c.queue.Len()
	g := gauges{QueueLen: uint64(qlen), QueueCap: uint64(qcap)}
	if c.spool != nil {
		n, b := c.spool.usage()
		g.SpoolRecords, g.SpoolBytes = uint64(n), uint64(b)
	}
	start, rows := c.stats.rollInterval(now, g, map[string]map[netflow.DomainKey]int{
		netflow.ProtocolNetFlow9: c.v9Templates.ActiveTemplates(),
		netflow.ProtocolIPFIX:    c.ipfixTemplates.ActiveTemplates(),
	})

	c.statsMu.Lock()
	c.statsPending = append(c.statsPending, pendingStats{
		Token: fmt.Sprintf("stats-%s-%s-%d", c.cfg.ID, c.bootID, start.UnixNano()),
		Rows:  rows,
	})
	if max := int(time.Hour / c.cfg.StatsInterval); len(c.statsPending) > max {
		c.statsPending = c.statsPending[len(c.statsPending)-max:]
	}
	pending := c.statsPending
	c.statsPending = nil
	c.statsMu.Unlock()

	var failed []pendingStats
	for i, p := range pending {
		ictx, cancel := opContext(ctx, c.cfg.InsertTimeout, &c.stopBy)
		err := prepareSchema(ictx, c.store, c.delivery)
		if err == nil {
			err = c.store.InsertStats(ictx, p.Token, p.Rows)
		}
		cancel()
		if err != nil {
			if classify(err) == errSchema {
				c.delivery.schemaSuspect()
			}
			failed = append(failed, pending[i:]...)
			c.logf(&c.lastStatsLog, "WARN: netflow collector statistics not written (%s); will retry", errorLabel(err))
			break
		}
	}
	c.statsMu.Lock()
	c.statsPending = append(failed, c.statsPending...)
	if len(failed) == 0 {
		c.statsFailedAt = time.Time{}
	} else if c.statsFailedAt.IsZero() {
		c.statsFailedAt = now
	}
	c.statsMu.Unlock()
}

// sampleKernel adds the host UDP RcvbufErrors delta to the process row and
// records this socket's own kernel drop counter for /health.
func (c *collector) sampleKernel(now time.Time) {
	c.kernelMu.Lock()
	defer c.kernelMu.Unlock()
	if v, ok := udpRcvbufErrors(); ok {
		if c.kernelBaseOK && v >= c.kernelBase {
			delta := v - c.kernelBase
			c.stats.processDelta(func(k *counters) { k.KernelRcvbufErrors += delta })
		}
		c.kernelBase, c.kernelBaseOK = v, true
	}
	var drops uint64
	found := false
	for _, inode := range c.sockInodes {
		if d, ok := socketDrops(inode); ok {
			drops, found = drops+d, true
		}
	}
	if found {
		c.socketDropsLast = drops
		c.socketDropsAt = append(c.socketDropsAt, socketDropSample{at: now, drops: drops})
		for len(c.socketDropsAt) > 1 && now.Sub(c.socketDropsAt[0].at) > 5*time.Minute {
			c.socketDropsAt = c.socketDropsAt[1:]
		}
	}
}

// finalFlushBudget is how long the last statistics/registry writes may take
// at shutdown (included in the unit's TimeoutStopSec).
const finalFlushBudget = 5 * time.Second

// finalFlush writes the last partial stats interval and the pending
// registry rows with a fresh context (the run context is already cancelled).
func (c *collector) finalFlush() {
	// The template caches go to local disk first: unlike the ClickHouse
	// writes below they cannot hang.
	c.saveTemplateState(time.Now(), true)
	ctx, cancel := context.WithTimeout(context.Background(), finalFlushBudget)
	defer cancel()
	c.flushStats(ctx, time.Now().UTC())
	c.flushRegistries(ctx)
	if c.spool == nil {
		return
	}
	c.statsMu.Lock()
	pending := c.statsPending
	c.statsMu.Unlock()
	if len(pending) == 0 {
		return
	}
	data, err := json.Marshal(pending)
	if err == nil {
		err = writeFileAtomic(filepath.Join(c.cfg.SpoolDir, statsPendingFile), data)
	}
	if err != nil {
		fmt.Printf("WARN: netflow collector statistics for %d intervals could not be kept: %v\n", len(pending), err)
	}
}

// loadPendingStats picks up the stats intervals a previous run could not
// write (see finalFlush).
func (c *collector) loadPendingStats() {
	path := filepath.Join(c.cfg.SpoolDir, statsPendingFile)
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	_ = os.Remove(path)
	var pending []pendingStats
	if err := json.Unmarshal(data, &pending); err != nil {
		fmt.Printf("WARN: netflow pending collector statistics unreadable: %v\n", err)
		return
	}
	c.statsMu.Lock()
	c.statsPending = append(pending, c.statsPending...)
	c.statsMu.Unlock()
}

func spoolRecords(s *diskSpool) int {
	if s == nil {
		return 0
	}
	n, _ := s.usage()
	return n
}

func spoolLabel(dir string) string {
	if dir == "" {
		return "memory only"
	}
	return dir
}

func newBootID() string {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		return strconv.FormatInt(time.Now().UnixNano(), 36)
	}
	return hex.EncodeToString(b)
}

func loadConfig() collectorConfig {
	policy := clockPolicy(strings.ToLower(env("NETFLOW_CLOCK_POLICY", string(clockClamp))))
	if policy != clockClamp && policy != clockTrust {
		fmt.Printf("WARN: NETFLOW_CLOCK_POLICY=%q is not clamp|trust; using clamp\n", policy)
		policy = clockClamp
	}
	return collectorConfig{
		ID:               env("NETFLOW_COLLECTOR_ID", "netflow-01"),
		Listen:           env("NETFLOW_LISTEN", ":2055"),
		SFlowListen:      envOptional("NETFLOW_SFLOW_LISTEN", ":6343"),
		HealthListen:     env("NETFLOW_HEALTH_LISTEN", "127.0.0.1:8091"),
		BatchSize:        envIntMin("NETFLOW_BATCH_SIZE", 1000, 1),
		FlushInterval:    envSeconds("NETFLOW_FLUSH_SECONDS", 5, 1),
		AllowedExporters: parseExporterAllowlist(env("NETFLOW_ALLOWED_EXPORTERS", "")),
		QueueMaxRecords:  envIntMin("NETFLOW_QUEUE_MAX_RECORDS", 100_000, 1),
		SpoolDir:         env("NETFLOW_SPOOL_DIR", ""),
		SpoolMaxBytes:    int64(envIntMin("NETFLOW_SPOOL_MAX_MB", 1024, 1)) << 20,
		InsertTimeout:    envSeconds("NETFLOW_INSERT_TIMEOUT_SECONDS", 30, 1),
		ShutdownTimeout:  envSeconds("NETFLOW_SHUTDOWN_TIMEOUT_SECONDS", 15, 1),
		StatsInterval:    envSeconds("NETFLOW_STATS_INTERVAL_SECONDS", 60, 5),
		RcvBuf:           envIntMin("NETFLOW_RCVBUF", 8<<20, 0),
		ClockPolicy:      policy,
		ClickHouseDSN: clickhouse.Options{
			Addr: []string{fmt.Sprintf("%s:%d", env("CLICKHOUSE_HOST", "localhost"), envIntMin("CLICKHOUSE_PORT", 9000, 1))},
			Auth: clickhouse.Auth{
				Database: env("CLICKHOUSE_DB", "zenplus"),
				Username: env("CLICKHOUSE_USER", "default"),
				Password: env("CLICKHOUSE_PASSWORD", "clickhouse_dev"),
			},
			Settings:    clickhouse.Settings{"max_execution_time": 60},
			Compression: &clickhouse.Compression{Method: clickhouse.CompressionLZ4},
		},
	}
}

// parseExporterAllowlist turns a comma/space-separated list of exporter IPs
// into a set of canonical addresses. A blank setting means "allow all" (nil,
// BUG-07). A setting that is present is always enforced, and an entry that is
// not an IP address is reported and ignored: a typo must never turn the
// allowlist into "accept everything", so a list with no valid entry at all
// accepts nothing (every datagram is then counted as from an unknown exporter).
func parseExporterAllowlist(s string) map[netip.Addr]bool {
	if strings.TrimSpace(s) == "" {
		return nil
	}
	out := make(map[netip.Addr]bool)
	for _, part := range strings.FieldsFunc(s, func(r rune) bool { return r == ',' || r == ' ' || r == '\t' || r == '\n' }) {
		a, err := netip.ParseAddr(strings.TrimSpace(part))
		if err != nil {
			fmt.Printf("WARN: NETFLOW_ALLOWED_EXPORTERS entry %q is not an IP address; ignored\n", part)
			continue
		}
		out[netflow.CanonicalAddr(a)] = true
	}
	if len(out) == 0 {
		fmt.Println("WARN: NETFLOW_ALLOWED_EXPORTERS has no valid address: no exporter is accepted")
	}
	return out
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// envOptional is env for a setting that can be switched off: unset means the
// default, set to empty (or "off") disables it.
func envOptional(key, fallback string) string {
	v, ok := os.LookupEnv(key)
	if !ok {
		return fallback
	}
	if v = strings.TrimSpace(v); strings.EqualFold(v, "off") {
		return ""
	}
	return v
}

// envIntMin reads an integer setting. A value that does not parse or is
// below min is reported and replaced by the default, so a typo cannot
// configure, for example, a zero insert timeout that fails every write.
func envIntMin(key string, fallback, min int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < min {
		fmt.Printf("WARN: %s=%q is not a whole number >= %d; using %d\n", key, v, min, fallback)
		return fallback
	}
	return n
}

func envSeconds(key string, fallback, min int) time.Duration {
	return time.Duration(envIntMin(key, fallback, min)) * time.Second
}

func fatalf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, format+"\n", args...)
	os.Exit(1)
}

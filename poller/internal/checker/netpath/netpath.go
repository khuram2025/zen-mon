package netpath

// The NetPath sub-service: a self-contained scheduler that loads probe
// definitions from Postgres, runs due traceroutes on a bounded worker pool, and
// persists each run. It is wired in cmd/poller/main.go alongside the ping
// engine and shares the poller's CAP_NET_RAW.

import (
	"context"
	"encoding/json"
	"net"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/google/uuid"
	"go.uber.org/zap"
)

// marshalJSON serializes hop/flow slices; on error returns an empty array so a
// bad run never blocks the snapshot insert.
func marshalJSON(v interface{}) []byte {
	b, err := json.Marshal(v)
	if err != nil || len(b) == 0 {
		return []byte("[]")
	}
	return b
}

// Protocol values.
const (
	ProtoICMP = "icmp"
	ProtoTCP  = "tcp"
	ProtoUDP  = "udp"
)

// Probe is a probe definition loaded from netpath_probes.
type Probe struct {
	ID            uuid.UUID
	Name          string
	TargetHost    string
	TargetIP      net.IP
	Port          int
	Protocol      string
	MaxHops       int
	ProbesPerHop  int
	Flows         int
	IntervalS     int
	RunNow        bool
	RttWarnMs     float64
	RttCritMs     float64
	LossWarnPct   float64
	LossCritPct   float64
	LastPathHash  int64
	HasLastHash   bool
	LastReached   bool
	HasLastReach  bool
}

// SaveInput bundles everything the store needs to persist one run.
type SaveInput struct {
	Probe      *Probe
	Result     *RunResult
	ResolvedIP net.IP
	Status     string
	HopsJSON   []byte
	FlowsJSON  []byte
	Vantage    string
	RanAt      time.Time
}

// Store is the persistence surface implemented by *store.PostgresStore
// (see internal/store/postgres_netpath.go).
type Store interface {
	LoadNetpathProbes(ctx context.Context) ([]*Probe, error)
	SaveNetpathRun(ctx context.Context, in *SaveInput) error
	ClearNetpathRunNow(ctx context.Context, id uuid.UUID) error
}

// Service schedules and runs traceroutes.
type Service struct {
	store   Store
	logger  *zap.SugaredLogger
	vantage string

	mu       sync.Mutex
	lastRun  map[uuid.UUID]time.Time
	inflight map[uuid.UUID]bool
	nonce    int
}

// NewService constructs the NetPath service.
func NewService(store Store, vantage string, logger *zap.SugaredLogger) *Service {
	if vantage == "" {
		vantage = "appliance"
	}
	return &Service{
		store:    store,
		logger:   logger,
		vantage:  vantage,
		lastRun:  make(map[uuid.UUID]time.Time),
		inflight: make(map[uuid.UUID]bool),
	}
}

// schedulerInterval is how often the scheduler wakes to look for due probes.
func schedulerInterval() time.Duration {
	if v := os.Getenv("NETPATH_TICK"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 {
			return time.Duration(n) * time.Second
		}
	}
	return 5 * time.Second
}

// maxConcurrent bounds simultaneous traces (each holds a few raw sockets).
func maxConcurrent() int {
	if v := os.Getenv("NETPATH_CONCURRENCY"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 1 && n <= 64 {
			return n
		}
	}
	return 8
}

// Run is the scheduler loop. It returns when ctx is cancelled.
func (s *Service) Run(ctx context.Context) {
	s.logger.Infof("NetPath service started (vantage=%s)", s.vantage)
	tick := time.NewTicker(schedulerInterval())
	defer tick.Stop()
	sem := make(chan struct{}, maxConcurrent())

	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			probes, err := s.store.LoadNetpathProbes(ctx)
			if err != nil {
				s.logger.Warnf("netpath: load probes: %v", err)
				continue
			}
			now := time.Now()
			for _, p := range probes {
				if !s.due(p, now) {
					continue
				}
				s.mark(p.ID, now)
				select {
				case sem <- struct{}{}:
				case <-ctx.Done():
					return
				}
				go func(pr *Probe) {
					defer func() { <-sem }()
					defer s.clearInflight(pr.ID)
					s.runProbe(ctx, pr)
				}(p)
			}
		}
	}
}

func (s *Service) due(p *Probe, now time.Time) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.inflight[p.ID] {
		return false
	}
	if p.RunNow {
		return true
	}
	last, ok := s.lastRun[p.ID]
	iv := time.Duration(p.IntervalS) * time.Second
	if iv <= 0 {
		iv = 5 * time.Minute
	}
	return !ok || now.Sub(last) >= iv
}

func (s *Service) mark(id uuid.UUID, now time.Time) {
	s.mu.Lock()
	s.inflight[id] = true
	s.lastRun[id] = now
	s.mu.Unlock()
}

func (s *Service) clearInflight(id uuid.UUID) {
	s.mu.Lock()
	delete(s.inflight, id)
	s.mu.Unlock()
}

func (s *Service) nextNonce() int {
	s.mu.Lock()
	s.nonce = (s.nonce + 1) & 7
	n := s.nonce
	s.mu.Unlock()
	return n
}

// runProbe resolves the target, runs the trace, computes status and persists.
func (s *Service) runProbe(ctx context.Context, p *Probe) {
	if p.RunNow {
		_ = s.store.ClearNetpathRunNow(ctx, p.ID)
	}
	runCtx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()

	ip := p.TargetIP
	if ip == nil {
		resolved, err := resolveIPv4(runCtx, p.TargetHost)
		if err != nil {
			s.saveError(ctx, p, nil, "resolve: "+err.Error())
			return
		}
		ip = resolved
	}

	cfg := RunConfig{
		Target:       ip,
		Port:         p.Port,
		Protocol:     p.Protocol,
		MaxHops:      p.MaxHops,
		ProbesPerHop: p.ProbesPerHop,
		Flows:        p.Flows,
		Nonce:        s.nextNonce(),
		ProbeTimeout: 2 * time.Second,
		SendGap:      time.Millisecond,
	}
	res, err := Trace(runCtx, cfg)
	if err != nil {
		s.saveError(ctx, p, ip, err.Error())
		return
	}

	status := deriveStatus(p, res)
	in := &SaveInput{
		Probe:      p,
		Result:     res,
		ResolvedIP: ip,
		Status:     status,
		HopsJSON:   marshalJSON(res.Hops),
		FlowsJSON:  marshalJSON(res.Flows),
		Vantage:    s.vantage,
		RanAt:      time.Now().UTC(),
	}
	if err := s.store.SaveNetpathRun(ctx, in); err != nil {
		s.logger.Warnf("netpath: save run for %s: %v", p.Name, err)
	}
}

func (s *Service) saveError(ctx context.Context, p *Probe, ip net.IP, msg string) {
	res := &RunResult{Reached: false, LossPct: 100, Error: msg, HopCount: 0, Hops: []Hop{}, Flows: []FlowPath{}}
	in := &SaveInput{
		Probe:      p,
		Result:     res,
		ResolvedIP: ip,
		Status:     "unreached",
		HopsJSON:   []byte("[]"),
		FlowsJSON:  []byte("[]"),
		Vantage:    s.vantage,
		RanAt:      time.Now().UTC(),
	}
	if err := s.store.SaveNetpathRun(ctx, in); err != nil {
		s.logger.Warnf("netpath: save error-run for %s: %v", p.Name, err)
	}
}

// deriveStatus colours the run against the probe's own thresholds.
func deriveStatus(p *Probe, r *RunResult) string {
	if !r.Reached {
		return "unreached"
	}
	crit := false
	warn := false
	if p.LossCritPct > 0 && r.LossPct >= p.LossCritPct {
		crit = true
	} else if p.LossWarnPct > 0 && r.LossPct >= p.LossWarnPct {
		warn = true
	}
	if p.RttCritMs > 0 && r.RttMs >= p.RttCritMs {
		crit = true
	} else if p.RttWarnMs > 0 && r.RttMs >= p.RttWarnMs {
		warn = true
	}
	switch {
	case crit:
		return "down"
	case warn:
		return "degraded"
	default:
		return "ok"
	}
}

func resolveIPv4(ctx context.Context, host string) (net.IP, error) {
	if ip := net.ParseIP(host); ip != nil {
		if v4 := ip.To4(); v4 != nil {
			return v4, nil
		}
	}
	ips, err := net.DefaultResolver.LookupIP(ctx, "ip4", host)
	if err != nil {
		return nil, err
	}
	if len(ips) == 0 {
		return nil, net.UnknownNetworkError("no A record")
	}
	return ips[0].To4(), nil
}

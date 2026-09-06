package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
	"github.com/zenplus/poller/internal/sensorspool"
	"go.uber.org/zap"
)

type blockingProbeRunner struct {
	release chan struct{}
	started chan string

	mu        sync.Mutex
	active    int
	maxActive int
	calls     map[string]int
}

type partialSaturationRunner struct {
	longRelease chan struct{}
	started     chan string

	mu    sync.Mutex
	calls map[string]int
}

func newPartialSaturationRunner() *partialSaturationRunner {
	return &partialSaturationRunner{
		longRelease: make(chan struct{}, 16),
		started:     make(chan string, 128),
		calls:       make(map[string]int),
	}
}

func (r *partialSaturationRunner) CheckOne(ctx context.Context, sc *checker.ServiceCheck, _ string) *checker.ServiceCheckResult {
	key := sc.TargetHost
	r.mu.Lock()
	r.calls[key]++
	r.mu.Unlock()
	r.started <- key
	if len(key) >= len("long-") && key[:len("long-")] == "long-" {
		select {
		case <-ctx.Done():
		case <-r.longRelease:
		}
	}
	return &checker.ServiceCheckResult{ServiceCheckID: sc.ID, CheckType: sc.CheckType, IsUp: true, Timestamp: time.Now().UTC()}
}

func newBlockingProbeRunner(buffer int) *blockingProbeRunner {
	return &blockingProbeRunner{
		release: make(chan struct{}, buffer),
		started: make(chan string, buffer),
		calls:   make(map[string]int),
	}
}

func (r *blockingProbeRunner) CheckOne(ctx context.Context, sc *checker.ServiceCheck, _ string) *checker.ServiceCheckResult {
	key := sc.TargetHost
	if key == "" {
		key = sc.ID.String()
	}
	r.mu.Lock()
	r.active++
	if r.active > r.maxActive {
		r.maxActive = r.active
	}
	r.calls[key]++
	r.mu.Unlock()
	r.started <- key
	select {
	case <-ctx.Done():
	case <-r.release:
	}
	r.mu.Lock()
	r.active--
	r.mu.Unlock()
	return &checker.ServiceCheckResult{
		ServiceCheckID: sc.ID, CheckType: sc.CheckType, IsUp: true,
		Timestamp: time.Now().UTC(),
	}
}

func (r *blockingProbeRunner) maximum() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.maxActive
}

func withoutSchedulerJitter(scheduler *checkScheduler) *checkScheduler {
	scheduler.jitter = func(string, time.Duration) time.Duration { return 0 }
	return scheduler
}

func TestSchedulerHonorsGlobalWorkerLimit(t *testing.T) {
	runner := newBlockingProbeRunner(10)
	scheduler := withoutSchedulerJitter(newCheckScheduler(runner, "test", 2, func(string, any) error { return nil }, zap.NewNop().Sugar()))
	now := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	current := configResponse{Devices: []configDevice{
		{ID: "1", IPAddress: "10.0.0.1", PingEnabled: true, PingInterval: 60},
		{ID: "2", IPAddress: "10.0.0.2", PingEnabled: true, PingInterval: 60},
		{ID: "3", IPAddress: "10.0.0.3", PingEnabled: true, PingInterval: 60},
	}}

	if got := scheduler.Schedule(context.Background(), current, now); got != 2 {
		t.Fatalf("started = %d, want 2", got)
	}
	waitForStarts(t, runner.started, 2)
	if got := scheduler.Schedule(context.Background(), current, now.Add(time.Second)); got != 0 {
		t.Fatalf("started while pool full = %d, want 0", got)
	}
	if got := runner.maximum(); got != 2 {
		t.Fatalf("maximum active probes = %d, want 2", got)
	}
	runner.release <- struct{}{}
	runner.release <- struct{}{}
	if !scheduler.Wait(time.Second) {
		t.Fatal("workers did not finish")
	}

	// The first two targets are not due again. The target skipped because the
	// pool was full remains due and starts on the next scheduler pass.
	if got := scheduler.Schedule(context.Background(), current, now.Add(2*time.Second)); got != 1 {
		t.Fatalf("started after capacity returned = %d, want 1", got)
	}
	waitForStarts(t, runner.started, 1)
	runner.release <- struct{}{}
	if !scheduler.Wait(time.Second) {
		t.Fatal("last worker did not finish")
	}
}

func TestSchedulerPreventsPerTargetOverlapAndHonorsInterval(t *testing.T) {
	runner := newBlockingProbeRunner(10)
	scheduler := withoutSchedulerJitter(newCheckScheduler(runner, "test", 4, func(string, any) error { return nil }, zap.NewNop().Sugar()))
	now := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	current := configResponse{Devices: []configDevice{
		{ID: "same", IPAddress: "10.0.0.1", PingEnabled: true, PingInterval: 1},
	}}

	if got := scheduler.Schedule(context.Background(), current, now); got != 1 {
		t.Fatalf("initial started = %d, want 1", got)
	}
	waitForStarts(t, runner.started, 1)
	if got := scheduler.Schedule(context.Background(), current, now.Add(2*time.Second)); got != 0 {
		t.Fatalf("overlapping started = %d, want 0", got)
	}
	runner.release <- struct{}{}
	if !scheduler.Wait(time.Second) {
		t.Fatal("worker did not finish")
	}

	if got := scheduler.Schedule(context.Background(), current, now.Add(500*time.Millisecond)); got != 0 {
		t.Fatalf("started before interval = %d, want 0", got)
	}
	if got := scheduler.Schedule(context.Background(), current, now.Add(time.Second)); got != 1 {
		t.Fatalf("started when interval elapsed = %d, want 1", got)
	}
	waitForStarts(t, runner.started, 1)
	runner.release <- struct{}{}
	if !scheduler.Wait(time.Second) {
		t.Fatal("second worker did not finish")
	}
}

func TestSchedulerUsesOnePoolForDeviceAndServiceChecks(t *testing.T) {
	runner := newBlockingProbeRunner(10)
	scheduler := withoutSchedulerJitter(newCheckScheduler(runner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar()))
	current := configResponse{
		Devices: []configDevice{{ID: "device", IPAddress: "10.0.0.1", PingEnabled: true, PingInterval: 60}},
		ServiceChecks: []configServiceCheck{{
			ID: uuid.NewString(), Name: "service", CheckType: "tcp", TargetHost: "10.0.0.2",
			TargetPort: 443, CheckInterval: 60, Timeout: 1, Enabled: true,
		}},
	}
	if got := scheduler.Schedule(context.Background(), current, time.Now()); got != 1 {
		t.Fatalf("started = %d, want one global worker", got)
	}
	waitForStarts(t, runner.started, 1)
	if got := runner.maximum(); got != 1 {
		t.Fatalf("maximum active probes = %d, want 1", got)
	}
	runner.release <- struct{}{}
	if !scheduler.Wait(time.Second) {
		t.Fatal("worker did not finish")
	}
}

func TestSchedulerIsFairUnderSustainedSaturation(t *testing.T) {
	runner := newBlockingProbeRunner(20)
	scheduler := withoutSchedulerJitter(newCheckScheduler(runner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar()))
	serviceID := uuid.NewString()
	current := configResponse{
		Devices: []configDevice{
			{ID: "d1", IPAddress: "10.0.0.1", PingEnabled: true, PingInterval: 1},
			{ID: "d2", IPAddress: "10.0.0.2", PingEnabled: true, PingInterval: 1},
			{ID: "d3", IPAddress: "10.0.0.3", PingEnabled: true, PingInterval: 1},
			{ID: "d4", IPAddress: "10.0.0.4", PingEnabled: true, PingInterval: 1},
		},
		ServiceChecks: []configServiceCheck{{
			ID: serviceID, Name: "service", CheckType: "tcp", TargetHost: "10.0.0.99",
			TargetPort: 443, CheckInterval: 1, Timeout: 1, Enabled: true,
		}},
	}
	start := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	seenService := false
	for cycle := 0; cycle < 5; cycle++ {
		if got := scheduler.Schedule(context.Background(), current, start.Add(time.Duration(cycle)*2*time.Second)); got != 1 {
			t.Fatalf("cycle %d started=%d, want 1", cycle, got)
		}
		select {
		case key := <-runner.started:
			seenService = seenService || key == "10.0.0.99"
		case <-time.After(time.Second):
			t.Fatalf("cycle %d did not start", cycle)
		}
		runner.release <- struct{}{}
		if !scheduler.Wait(time.Second) {
			t.Fatalf("cycle %d did not finish", cycle)
		}
	}
	if !seenService {
		t.Fatal("service check starved behind continuously due devices")
	}
}

func TestSchedulerIsFairUnderPartialSaturation(t *testing.T) {
	runner := newPartialSaturationRunner()
	scheduler := withoutSchedulerJitter(newCheckScheduler(runner, "test", 10, func(string, any) error { return nil }, zap.NewNop().Sugar()))
	longChecks := make([]configDevice, 0, 9)
	for i := 0; i < 9; i++ {
		longChecks = append(longChecks, configDevice{
			ID: fmt.Sprintf("long-%d", i), IPAddress: fmt.Sprintf("long-%d", i), PingEnabled: true, PingInterval: 1,
		})
	}
	base := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	if got := scheduler.Schedule(context.Background(), configResponse{Devices: longChecks}, base); got != 9 {
		t.Fatalf("long-running probes started=%d, want 9", got)
	}
	waitForStarts(t, runner.started, 9)

	allChecks := append([]configDevice(nil), longChecks...)
	for i := 0; i < 11; i++ {
		allChecks = append(allChecks, configDevice{
			ID: fmt.Sprintf("short-%d", i), IPAddress: fmt.Sprintf("short-%d", i), PingEnabled: true, PingInterval: 1,
		})
	}
	seen := make(map[string]struct{})
	for cycle := 0; cycle < 11; cycle++ {
		if got := scheduler.Schedule(context.Background(), configResponse{Devices: allChecks}, base.Add(time.Duration(cycle+1)*2*time.Second)); got != 1 {
			t.Fatalf("cycle %d started=%d, want the one free worker", cycle, got)
		}
		select {
		case key := <-runner.started:
			if len(key) < len("short-") || key[:len("short-")] != "short-" {
				t.Fatalf("cycle %d unexpectedly restarted %q", cycle, key)
			}
			seen[key] = struct{}{}
		case <-time.After(time.Second):
			t.Fatalf("cycle %d did not start", cycle)
		}
		waitForWorkerCount(t, scheduler, 9)
	}
	if len(seen) != 11 {
		t.Fatalf("partial saturation starved checks: saw %d/11: %+v", len(seen), seen)
	}
	for i := 0; i < 9; i++ {
		runner.longRelease <- struct{}{}
	}
	if !scheduler.Wait(time.Second) {
		t.Fatal("long-running workers did not finish")
	}
}

func TestSchedulerAppliesStableInitialJitter(t *testing.T) {
	const interval = 5 * time.Minute
	key := ""
	offset := time.Duration(0)
	for i := 0; i < 100; i++ {
		candidate := fmt.Sprintf("device:%d", i)
		candidateOffset := stableProbeJitter("branch-a", candidate, interval)
		if candidateOffset > time.Millisecond {
			key, offset = candidate, candidateOffset
			break
		}
	}
	if key == "" || offset >= 30*time.Second || stableProbeJitter("branch-a", key, interval) != offset {
		t.Fatalf("unstable or out-of-range jitter: key=%q offset=%s", key, offset)
	}

	runner := newBlockingProbeRunner(2)
	scheduler := newCheckScheduler(runner, "branch-a", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	deviceID := key[len("device:"):]
	current := configResponse{Devices: []configDevice{{
		ID: deviceID, IPAddress: "10.0.0.1", PingEnabled: true, PingInterval: int(interval / time.Second),
	}}}
	base := time.Date(2026, 9, 1, 12, 0, 0, 0, time.UTC)
	if got := scheduler.Schedule(context.Background(), current, base); got != 0 {
		t.Fatalf("probe ignored initial jitter: started=%d", got)
	}
	if got := scheduler.Schedule(context.Background(), current, base.Add(offset-time.Nanosecond)); got != 0 {
		t.Fatalf("probe started before jitter elapsed: started=%d", got)
	}
	if got := scheduler.Schedule(context.Background(), current, base.Add(offset)); got != 1 {
		t.Fatalf("probe did not start at stable offset: started=%d offset=%s", got, offset)
	}
	waitForStarts(t, runner.started, 1)
	runner.release <- struct{}{}
	if !scheduler.Wait(time.Second) {
		t.Fatal("jittered worker did not finish")
	}
}

func TestConfigCacheRequiresPayloadForPersistedETag(t *testing.T) {
	legacy := newConfigCache(&state{ETag: "legacy-etag"})
	if got := legacy.ETag(); got != "" {
		t.Fatalf("legacy ETag without payload = %q, want empty", got)
	}
	if _, ok := legacy.Snapshot(); ok {
		t.Fatal("legacy state unexpectedly exposed a config")
	}

	followRedirects := false
	want := configResponse{
		ETag: "cached-etag", SensorID: "sensor-1", Devices: []configDevice{{ID: "device-1"}},
		ServiceChecks: []configServiceCheck{{
			ID: "service-1", HTTPHeaders: map[string]string{"X-ZenPlus": "edge"},
			Config:              map[string]any{"dns": map[string]any{"record_type": "AAAA"}},
			HTTPFollowRedirects: &followRedirects,
		}},
	}
	cache := newConfigCache(&state{ETag: want.ETag, LastGoodConfig: &want})
	got, ok := cache.Snapshot()
	if !ok || cache.ETag() != want.ETag || len(got.Devices) != 1 || len(got.ServiceChecks) != 1 {
		t.Fatalf("cached config not restored: ok=%v etag=%q config=%+v", ok, cache.ETag(), got)
	}
	got.Devices[0].ID = "mutated"
	got.ServiceChecks[0].HTTPHeaders["X-ZenPlus"] = "mutated"
	got.ServiceChecks[0].Config["dns"].(map[string]any)["record_type"] = "A"
	*got.ServiceChecks[0].HTTPFollowRedirects = true
	again, _ := cache.Snapshot()
	if again.Devices[0].ID != "device-1" || again.ServiceChecks[0].HTTPHeaders["X-ZenPlus"] != "edge" ||
		again.ServiceChecks[0].Config["dns"].(map[string]any)["record_type"] != "AAAA" ||
		*again.ServiceChecks[0].HTTPFollowRedirects {
		t.Fatal("callers can mutate the published config snapshot")
	}

	mismatched := newConfigCache(&state{SensorID: "sensor-2", ETag: want.ETag, LastGoodConfig: &want})
	if _, ok := mismatched.Snapshot(); ok || mismatched.ETag() != "" {
		t.Fatal("config cached for another sensor identity was accepted")
	}
}

func TestRuntimeConfigRequiresHTTPSControllerOrigin(t *testing.T) {
	for _, invalid := range []string{
		"http://controller.example",
		"https://user@controller.example",
		"https://controller.example/zenplus",
		"https://controller.example?tenant=one",
		"not-a-url",
	} {
		t.Setenv("ZENPLUS_SERVER_URL", invalid)
		if _, err := loadConfig(); err == nil {
			t.Fatalf("unsafe controller URL was accepted: %q", invalid)
		}
	}
	t.Setenv("ZENPLUS_SERVER_URL", "https://controller.example/")
	if cfg, err := loadConfig(); err != nil || cfg.serverURL != "https://controller.example/" {
		t.Fatalf("valid HTTPS controller origin rejected: cfg=%+v err=%v", cfg, err)
	}
}

func TestStatePersistsLastGoodConfigAcrossRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	first := configResponse{
		ETag: "etag-1", SensorID: "sensor-1",
		Devices: []configDevice{{ID: "device-1", IPAddress: "10.0.0.1", PingEnabled: true}},
	}
	st := &state{SensorID: "sensor-1", APIKey: "secret", ETag: first.ETag, LastGoodConfig: &first}
	if err := saveState(path, st); err != nil {
		t.Fatal(err)
	}

	// Exercise replacement as well as first creation; the config and matching
	// ETag must move together in the same state-file update.
	second := cloneConfig(first)
	second.ETag = "etag-2"
	second.Devices[0].IPAddress = "10.0.0.2"
	st.ETag = second.ETag
	st.LastGoodConfig = &second
	if err := saveState(path, st); err != nil {
		t.Fatal(err)
	}

	recovered, err := loadState(path)
	if err != nil {
		t.Fatal(err)
	}
	cache := newConfigCache(recovered)
	config, ok := cache.Snapshot()
	if !ok || cache.ETag() != "etag-2" || config.Devices[0].IPAddress != "10.0.0.2" {
		t.Fatalf("recovered state mismatch: ok=%v etag=%q config=%+v", ok, cache.ETag(), config)
	}
}

func TestControllerCertificatePinRequiresNormalVerification(t *testing.T) {
	leaf := &x509.Certificate{Raw: []byte("controller-leaf-certificate")}
	root := &x509.Certificate{Raw: []byte("controller-root-certificate")}
	digest := sha256.Sum256(root.Raw)
	pin := fmt.Sprintf("%x", digest[:])
	st := &state{ControllerCASHA256: pin}
	httpClient, err := newSensorHTTPClient(st)
	if err != nil {
		t.Fatal(err)
	}
	transport := httpClient.Transport.(*http.Transport)
	if transport.TLSClientConfig.InsecureSkipVerify {
		t.Fatal("persisted controller pin did not force normal CA verification")
	}
	verified := tls.ConnectionState{
		PeerCertificates: []*x509.Certificate{leaf},
		VerifiedChains:   [][]*x509.Certificate{{leaf, root}},
	}
	if err := verifyPinnedPeer(pin, verified); err != nil {
		t.Fatalf("matching normally verified leaf was rejected: %v", err)
	}
	wrongRoot := &x509.Certificate{Raw: []byte("different-controller-root")}
	verified.VerifiedChains = [][]*x509.Certificate{{leaf, wrongRoot}}
	if err := verifyPinnedPeer(pin, verified); err == nil {
		t.Fatal("mismatched controller trust anchor was accepted")
	}
	verified.VerifiedChains = nil
	if err := verifyPinnedPeer(pin, verified); err == nil {
		t.Fatal("pin replaced normal CA verification instead of augmenting it")
	}
}

func TestSensorCannotDisableTLSVerification(t *testing.T) {
	t.Setenv("ZENPLUS_SERVER_URL", "https://controller.example")
	t.Setenv("ZENPLUS_VERIFY_TLS", "0")
	if _, err := loadConfig(); err != nil {
		t.Fatal(err)
	}
	for _, st := range []*state{{}, {SensorID: "sensor-1", APIKey: "secret"}} {
		httpClient, err := newSensorHTTPClient(st)
		if err != nil {
			t.Fatal(err)
		}
		tlsConfig := httpClient.Transport.(*http.Transport).TLSClientConfig
		if tlsConfig.InsecureSkipVerify {
			t.Fatal("ZENPLUS_VERIFY_TLS=0 disabled controller CA validation")
		}
		if tlsConfig.MinVersion < tls.VersionTLS12 {
			t.Fatal("controller transport allows TLS older than 1.2")
		}
	}
}

func TestEnrollmentPersistsControllerCertificatePin(t *testing.T) {
	leafDigest := sha256.Sum256([]byte("enrollment-controller-leaf"))
	pin := fmt.Sprintf("%x", leafDigest[:])
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/sensor/enroll" {
			http.Error(w, "wrong path", http.StatusNotFound)
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"sensor_id": "sensor-1", "api_key": "secret", "controller_ca_sha256": pin,
		})
	}))
	defer server.Close()
	st := &state{}
	c := &client{baseURL: server.URL, httpClient: server.Client(), state: st}
	if err := c.enroll(&runtimeConfig{enrollmentToken: "token"}); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "state.json")
	if err := saveState(path, st); err != nil {
		t.Fatal(err)
	}
	recovered, err := loadState(path)
	if err != nil {
		t.Fatal(err)
	}
	if recovered.SensorID != "sensor-1" || recovered.APIKey != "secret" || recovered.ControllerCASHA256 != pin {
		t.Fatalf("enrollment trust state was not persisted: %+v", recovered)
	}
}

func TestClearEnrollmentTokenIsAtomicAndPreservesMode(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sensor.env")
	wantMode := os.FileMode(0640)
	before := "ZENPLUS_SERVER_URL='https://controller.example'\nZENPLUS_ENROLLMENT_TOKEN='one-time-secret'\nZENPLUS_SENSOR_NAME='branch-a'\n"
	if err := os.WriteFile(path, []byte(before), wantMode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, wantMode); err != nil {
		t.Fatal(err)
	}
	if err := clearEnrollmentToken(path); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), "one-time-secret") || !strings.Contains(string(data), "ZENPLUS_ENROLLMENT_TOKEN=''\n") ||
		!strings.Contains(string(data), "ZENPLUS_SERVER_URL='https://controller.example'") {
		t.Fatalf("unexpected rewritten environment: %q", data)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" && info.Mode().Perm() != wantMode {
		t.Fatalf("rewritten mode = %o, want %o", info.Mode().Perm(), wantMode)
	}
}

func TestHeartbeatCommandsExecuteOnceAndPersistOutcomes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	initial := &state{SensorID: "sensor-1", APIKey: "secret"}
	if err := saveState(path, initial); err != nil {
		t.Fatal(err)
	}
	store := newPersistedState(path, initial)
	reloadConfig := make(chan struct{}, 1)
	flushBuffer := make(chan struct{}, 1)
	level := zap.NewAtomicLevelAt(zap.InfoLevel)
	type capturedEvent struct {
		path string
		body map[string]any
	}
	var events []capturedEvent
	enqueue := func(path string, payload any) error {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return err
		}
		var body map[string]any
		if err := json.Unmarshal(encoded, &body); err != nil {
			return err
		}
		events = append(events, capturedEvent{path: path, body: body})
		return nil
	}
	processor := newCommandProcessor(zap.NewNop().Sugar(), level, &client{}, store, enqueue, reloadConfig, flushBuffer, func() {})
	processor.Process(context.Background(), []heartbeatCommand{
		{ID: "reload-1", Verb: "reload_config"},
		{ID: "flush-1", Verb: "flush_buffer"},
		{ID: "log-1", Verb: "set_log_level", Payload: json.RawMessage(`{"level":"debug"}`)},
		{ID: "unknown-1", Verb: "future_command"},
	})

	if len(reloadConfig) != 1 || len(flushBuffer) != 1 || level.Level() != zap.DebugLevel {
		t.Fatalf("command effects missing: reload=%d flush=%d level=%s", len(reloadConfig), len(flushBuffer), level.Level())
	}
	if len(events) != 4 {
		t.Fatalf("outcome events=%d, want 4", len(events))
	}
	for _, event := range events {
		if event.path != eventsPath {
			t.Fatalf("command outcome path=%q, want %q", event.path, eventsPath)
		}
	}
	if events[3].body["type"] != "command_failed" {
		t.Fatalf("unsupported command outcome=%+v", events[3].body)
	}
	recovered, err := loadState(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(recovered.CompletedCommands) != 4 {
		t.Fatalf("persisted completed commands=%d, want 4", len(recovered.CompletedCommands))
	}

	<-reloadConfig
	processor.Process(context.Background(), []heartbeatCommand{{ID: "reload-1", Verb: "reload_config"}})
	if len(reloadConfig) != 0 {
		t.Fatal("duplicate command was executed again")
	}
	if len(events) != 5 || events[4].body["type"] != "command_completed" {
		t.Fatalf("duplicate outcome was not re-emitted: %+v", events)
	}
}

func TestHeartbeatDoesNotBlockOnCommandProcessing(t *testing.T) {
	var mu sync.Mutex
	heartbeats := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mu.Lock()
		heartbeats++
		mu.Unlock()
		_ = json.NewEncoder(w).Encode(heartbeatResponse{Commands: []heartbeatCommand{{ID: "update-1", Verb: "update"}}})
	}))
	defer server.Close()

	ctx, cancel := context.WithCancel(context.Background())
	commands := make(chan []heartbeatCommand, 1)
	done := make(chan struct{})
	spool := mustOpenTestSpool(t)
	go func() {
		defer close(done)
		heartbeatLoop(ctx, zap.NewNop().Sugar(), &runtimeConfig{
			heartbeatEvery: 15 * time.Millisecond, startedAt: time.Now(),
		}, &client{
			baseURL: server.URL, httpClient: server.Client(), state: &state{SensorID: "sensor-1", APIKey: "secret"},
		}, spool, &runtimeAuthorization{}, commands)
	}()

	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		count := heartbeats
		mu.Unlock()
		if count >= 3 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	<-done
	mu.Lock()
	count := heartbeats
	mu.Unlock()
	if count < 3 || len(commands) != 1 {
		t.Fatalf("heartbeat stalled behind undrained command queue: calls=%d queued=%d", count, len(commands))
	}
}

func mustOpenTestSpool(t *testing.T) *sensorspool.Spool {
	t.Helper()
	spool, err := sensorspool.Open(filepath.Join(t.TempDir(), "wal"), sensorspool.Options{MaxBytes: 1 << 20, MaxAge: time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	return spool
}

func TestCompletedCommandCacheIsBounded(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	initial := &state{SensorID: "sensor-1", APIKey: "secret"}
	if err := saveState(path, initial); err != nil {
		t.Fatal(err)
	}
	processor := newCommandProcessor(
		zap.NewNop().Sugar(), zap.NewAtomicLevel(), &client{}, newPersistedState(path, initial),
		func(string, any) error { return nil }, make(chan struct{}, 1), make(chan struct{}, 1), func() {},
	)
	for i := 0; i < completedCommandLimit+5; i++ {
		if err := processor.remember(completedCommand{ID: fmt.Sprintf("command-%03d", i), Verb: "reload_config"}); err != nil {
			t.Fatal(err)
		}
	}
	recovered, err := loadState(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(recovered.CompletedCommands) != completedCommandLimit || recovered.CompletedCommands[0].ID != "command-005" {
		t.Fatalf("bounded cache mismatch: length=%d first=%q", len(recovered.CompletedCommands), recovered.CompletedCommands[0].ID)
	}
}

func TestUpdateCommandStopsAfterDurableOutcome(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	initial := &state{SensorID: "sensor-1", APIKey: "secret"}
	if err := saveState(path, initial); err != nil {
		t.Fatal(err)
	}
	ctx, stop := context.WithCancel(context.Background())
	updateCalls := 0
	events := 0
	processor := newCommandProcessor(
		zap.NewNop().Sugar(), zap.NewAtomicLevel(), &client{}, newPersistedState(path, initial),
		func(path string, _ any) error {
			if path != eventsPath {
				t.Fatalf("outcome path=%q", path)
			}
			events++
			return nil
		}, make(chan struct{}, 1), make(chan struct{}, 1), stop,
	)
	processor.applyUpdate = func(context.Context, heartbeatCommand) (string, error) {
		updateCalls++
		return "sensor-0.2.0", nil
	}
	command := heartbeatCommand{ID: "update-1", Verb: "update", Payload: json.RawMessage(`{"manifest_url":"https://controller.example/update.json"}`)}
	processor.Process(ctx, []heartbeatCommand{command})
	if ctx.Err() == nil || updateCalls != 1 || events != 1 {
		t.Fatalf("update effect mismatch: stopped=%v calls=%d events=%d", ctx.Err() != nil, updateCalls, events)
	}
	recovered, err := loadState(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(recovered.CompletedCommands) != 1 || recovered.CompletedCommands[0].Version != "sensor-0.2.0" ||
		recovered.CompletedCommands[0].EventType != "command_completed" {
		t.Fatalf("update outcome not durably cached: %+v", recovered.CompletedCommands)
	}

	second := newCommandProcessor(
		zap.NewNop().Sugar(), zap.NewAtomicLevel(), &client{}, newPersistedState(path, recovered),
		func(string, any) error { events++; return nil }, make(chan struct{}, 1), make(chan struct{}, 1), func() {},
	)
	second.applyUpdate = func(context.Context, heartbeatCommand) (string, error) {
		updateCalls++
		return "", errors.New("duplicate update executed")
	}
	second.Process(context.Background(), []heartbeatCommand{command})
	if updateCalls != 1 || events != 2 {
		t.Fatalf("durable dedupe mismatch: update calls=%d events=%d", updateCalls, events)
	}
}

func TestUpdateManifestAndDownloadURLValidation(t *testing.T) {
	if compareNumericVersions("sensor-0.2.0+abc123", "sensor-0.2.0") != 0 {
		t.Fatal("release commit suffix changed semver-core equality")
	}
	checksum := strings.Repeat("a", sha256.Size*2)
	valid := sensorUpdateManifest{
		Version: "sensor-0.2.0", OS: "linux", Arch: "amd64",
		BinaryURL: "https://controller.example/releases/sensor", SHA256: checksum,
	}
	if err := validateUpdateManifest(valid, "sensor-0.2.0", "sensor-0.1.0", "linux", "amd64"); err != nil {
		t.Fatalf("valid manifest rejected: %v", err)
	}
	invalid := []sensorUpdateManifest{
		{Version: "newest", OS: "linux", Arch: "amd64", BinaryURL: valid.BinaryURL, SHA256: checksum},
		{Version: "sensor-0.1.0", OS: "linux", Arch: "amd64", BinaryURL: valid.BinaryURL, SHA256: checksum},
		{Version: "sensor-0.2.0", OS: "windows", Arch: "amd64", BinaryURL: valid.BinaryURL, SHA256: checksum},
		{Version: "sensor-0.2.0", OS: "linux", Arch: "amd64", BinaryURL: valid.BinaryURL, SHA256: "not-a-digest"},
	}
	for i, manifest := range invalid {
		if err := validateUpdateManifest(manifest, "", "sensor-0.1.0", "linux", "amd64"); err == nil {
			t.Fatalf("invalid manifest %d was accepted: %+v", i, manifest)
		}
	}

	controller := "https://controller.example"
	resolved, err := resolveUpdateBinaryURL("https://controller.example/releases/manifest.json", "zenplus-sensor")
	if err != nil || resolved != "https://controller.example/releases/zenplus-sensor" {
		t.Fatalf("relative signed binary URL resolved to %q, err=%v", resolved, err)
	}
	if err := validateControllerDownloadURL(controller, "https://controller.example:443/releases/sensor"); err != nil {
		t.Fatalf("same HTTPS origin rejected: %v", err)
	}
	for _, candidate := range []string{
		"http://controller.example/releases/sensor",
		"https://downloads.example/releases/sensor",
		"https://controller.example:8443/releases/sensor",
		"https://attacker@controller.example/releases/sensor",
	} {
		if err := validateControllerDownloadURL(controller, candidate); err == nil {
			t.Fatalf("unsafe download URL accepted: %s", candidate)
		}
	}
}

func TestSignedUpdateManifestRequiresValidEd25519Signature(t *testing.T) {
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signed := []byte(`{"version":"sensor-0.2.0","os":"linux","arch":"amd64","binary_url":"https://controller.example/sensor","sha256":"` + strings.Repeat("a", sha256.Size*2) + `"}`)
	signature := ed25519.Sign(privateKey, signed)
	envelope, err := json.Marshal(signedUpdateManifestEnvelope{
		SignedManifest: base64.StdEncoding.EncodeToString(signed),
		Signature:      base64.StdEncoding.EncodeToString(signature),
	})
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := verifySignedUpdateManifest(envelope, publicKey)
	if err != nil || manifest.Version != "sensor-0.2.0" || manifest.BinaryURL != "https://controller.example/sensor" {
		t.Fatalf("valid signed manifest rejected: manifest=%+v err=%v", manifest, err)
	}

	tampered := append([]byte(nil), signed...)
	tampered[len(tampered)-2] ^= 1
	tamperedEnvelope, _ := json.Marshal(signedUpdateManifestEnvelope{
		SignedManifest: base64.StdEncoding.EncodeToString(tampered),
		Signature:      base64.StdEncoding.EncodeToString(signature),
	})
	if _, err := verifySignedUpdateManifest(tamperedEnvelope, publicKey); err == nil {
		t.Fatal("tampered signed manifest was accepted")
	}
	if _, err := verifySignedUpdateManifest([]byte(`{"version":"sensor-0.2.0"}`), publicKey); err == nil {
		t.Fatal("unsigned manifest fallback was accepted")
	}
	embedded, err := releasePublicKey()
	if err != nil || len(embedded) != ed25519.PublicKeySize {
		t.Fatalf("embedded release key is invalid: length=%d err=%v", len(embedded), err)
	}
}

func TestInstallUpdateChecksumMismatchLeavesExecutableUntouched(t *testing.T) {
	directory := t.TempDir()
	executable := filepath.Join(directory, "zenplus-sensor")
	original := []byte("original sensor binary")
	if err := os.WriteFile(executable, original, 0750); err != nil {
		t.Fatal(err)
	}
	wanted := sha256.Sum256([]byte("different binary"))
	err := installUpdateBinary(context.Background(), executable, bytes.NewReader([]byte("corrupt download")), hex.EncodeToString(wanted[:]), "sensor-0.2.0")
	if err == nil || !strings.Contains(err.Error(), "SHA-256 mismatch") {
		t.Fatalf("checksum mismatch error=%v", err)
	}
	after, readErr := os.ReadFile(executable)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if !bytes.Equal(after, original) {
		t.Fatalf("checksum failure changed executable: %q", after)
	}
	if _, statErr := os.Stat(executable + ".previous"); !errors.Is(statErr, os.ErrNotExist) {
		t.Fatalf("checksum failure created rollback artifact: %v", statErr)
	}
}

func TestUploadRetryDelayHonorsCadenceBackoffAndRetryAfter(t *testing.T) {
	if got := uploadRetryDelay(10*time.Second, 1, 0); got != 10*time.Second {
		t.Fatalf("first delay = %s", got)
	}
	if got := uploadRetryDelay(10*time.Second, 3, 0); got != 40*time.Second {
		t.Fatalf("third delay = %s", got)
	}
	if got := uploadRetryDelay(10*time.Second, 1, 90*time.Second); got != 90*time.Second {
		t.Fatalf("Retry-After delay = %s", got)
	}
	if got := uploadRetryDelay(10*time.Second, 20, time.Hour); got != 15*time.Minute {
		t.Fatalf("capped delay = %s", got)
	}
}

func TestRuntimeAuthorizationPausesOnlyForAuthoritativeAuthFailures(t *testing.T) {
	logger := zap.NewNop().Sugar()
	authorization := &runtimeAuthorization{}
	authorization.Observe(authorization.Begin(), &responseError{statusCode: http.StatusForbidden, body: "Sensor is not assigned to device"}, true, logger)
	if authorization.Blocked() {
		t.Fatal("assignment rejection incorrectly paused all probes")
	}
	authorization.Observe(authorization.Begin(), &responseError{statusCode: http.StatusForbidden, body: "Sensor disabled"}, true, logger)
	if !authorization.Blocked() {
		t.Fatal("disabled sensor did not pause probes")
	}
	authorization.Observe(authorization.Begin(), nil, true, logger)
	if authorization.Blocked() {
		t.Fatal("successful authentication did not resume probes")
	}
	authorization.Observe(authorization.Begin(), &responseError{statusCode: http.StatusUnauthorized, body: "Invalid sensor api key"}, true, logger)
	if !authorization.Blocked() {
		t.Fatal("revoked/invalid credentials did not pause probes")
	}
}

func TestRuntimeAuthorizationIgnoresOutOfOrderSuccess(t *testing.T) {
	logger := zap.NewNop().Sugar()
	authorization := &runtimeAuthorization{}
	staleSuccess := authorization.Begin()
	disabled := authorization.Begin()
	authorization.Observe(disabled, &responseError{statusCode: http.StatusForbidden, body: "Sensor disabled"}, true, logger)
	authorization.Observe(staleSuccess, nil, true, logger)
	if !authorization.Blocked() {
		t.Fatal("a success started before the disabled response resumed probes")
	}

	// Heartbeat success proves credentials work but cannot clear the sticky
	// disabled state. A config request started after the block can.
	authorization.Observe(authorization.Begin(), nil, false, logger)
	if !authorization.Blocked() {
		t.Fatal("heartbeat success cleared the disabled state")
	}
	authorization.Observe(authorization.Begin(), nil, true, logger)
	if authorization.Blocked() {
		t.Fatal("fresh config success did not clear the disabled state")
	}
}

func TestRuntimeAuthorizationNeverIgnoresExplicitRejection(t *testing.T) {
	logger := zap.NewNop().Sugar()
	authorization := &runtimeAuthorization{}
	olderRejection := authorization.Begin()
	laterStartedSuccess := authorization.Begin()
	authorization.Observe(laterStartedSuccess, nil, true, logger)
	authorization.Observe(olderRejection, &responseError{statusCode: http.StatusForbidden, body: "Sensor disabled"}, true, logger)
	if !authorization.Blocked() {
		t.Fatal("explicit rejection was ignored after a later-started success")
	}
}

func TestRuntimeAuthorizationBlockPersistsAcrossRestart(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	config := configResponse{ETag: "etag", SensorID: "sensor-1", Devices: []configDevice{{ID: "device-1"}}}
	initial := &state{SensorID: "sensor-1", APIKey: "secret", ETag: config.ETag, LastGoodConfig: &config}
	if err := saveState(path, initial); err != nil {
		t.Fatal(err)
	}
	store := newPersistedState(path, initial)
	authorization := newRuntimeAuthorization(false, func(blocked bool) error {
		return store.Update(func(next *state) { next.AuthorizationBlocked = blocked })
	})
	authorization.Observe(authorization.Begin(), &responseError{
		statusCode: http.StatusForbidden, body: "Sensor disabled",
	}, true, zap.NewNop().Sugar())
	if !authorization.Blocked() {
		t.Fatal("authorization rejection did not block the running scheduler")
	}

	recovered, err := loadState(path)
	if err != nil {
		t.Fatal(err)
	}
	if !recovered.AuthorizationBlocked || recovered.LastGoodConfig == nil || recovered.LastGoodConfig.ETag != config.ETag {
		t.Fatalf("persisted blocked state/config mismatch: %+v", recovered)
	}
	restartedStore := newPersistedState(path, recovered)
	restarted := newRuntimeAuthorization(recovered.AuthorizationBlocked, func(blocked bool) error {
		return restartedStore.Update(func(next *state) { next.AuthorizationBlocked = blocked })
	})
	if !restarted.Blocked() {
		t.Fatal("restart bypassed the persisted authorization block")
	}
	restarted.Observe(restarted.Begin(), nil, true, zap.NewNop().Sugar())
	if restarted.Blocked() {
		t.Fatal("fresh config success did not clear the persisted block")
	}
	cleared, err := loadState(path)
	if err != nil {
		t.Fatal(err)
	}
	if cleared.AuthorizationBlocked {
		t.Fatal("cleared authorization state was not persisted")
	}
}

func TestDrainSpoolIsolatesPermanentAssignmentRejection(t *testing.T) {
	spool, err := sensorspool.Open(filepath.Join(t.TempDir(), "wal"), sensorspool.Options{
		MaxBytes: 1 << 20, MaxAge: time.Hour,
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, item := range []struct {
		id     string
		device string
	}{{"stale", "stale-device"}, {"valid", "valid-device"}} {
		payload, _ := json.Marshal(map[string]any{"device_id": item.device, "is_up": true})
		if retained, err := spool.Enqueue(sensorspool.Item{ID: item.id, Path: pingResultsPath, Payload: payload}); err != nil || !retained {
			t.Fatalf("enqueue %s retained=%v err=%v", item.id, retained, err)
		}
	}

	requests := 0
	inserted := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		var body struct {
			Items []map[string]any `json:"items"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode request: %v", err)
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}
		for _, item := range body.Items {
			if item["device_id"] == "stale-device" {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte(`{"detail":"Sensor is not assigned to device(s)"}`))
				return
			}
		}
		inserted += len(body.Items)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	c := &client{baseURL: server.URL, httpClient: server.Client(), state: &state{SensorID: "sensor", APIKey: "key"}}
	if _, err := drainSpool(context.Background(), time.Second, c, spool, zap.NewNop().Sugar()); err != nil {
		t.Fatal(err)
	}
	stats := spool.Stats()
	if stats.Depth != 0 || stats.Dropped != 1 || inserted != 1 || requests < 2 {
		t.Fatalf("stats=%+v inserted=%d requests=%d", stats, inserted, requests)
	}
}

func TestDrainSpoolAllowsSlowRequestBeyondTurnBudget(t *testing.T) {
	spool, err := sensorspool.Open(filepath.Join(t.TempDir(), "wal"), sensorspool.Options{
		MaxBytes: 1 << 20, MaxAge: time.Hour,
	})
	if err != nil {
		t.Fatal(err)
	}
	if retained, err := spool.Enqueue(sensorspool.Item{
		ID: "slow", Path: pingResultsPath, Payload: json.RawMessage(`{"device_id":"device"}`),
	}); err != nil || !retained {
		t.Fatalf("enqueue retained=%v err=%v", retained, err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		time.Sleep(2100 * time.Millisecond) // exceeds drainSpool's two-second minimum turn budget
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	httpClient := server.Client()
	httpClient.Timeout = 3 * time.Second
	c := &client{baseURL: server.URL, httpClient: httpClient, state: &state{SensorID: "sensor", APIKey: "key"}}
	if _, err := drainSpool(context.Background(), time.Second, c, spool, zap.NewNop().Sugar()); err != nil {
		t.Fatal(err)
	}
	if got := spool.Stats().Depth; got != 0 {
		t.Fatalf("slow acknowledged request remained queued: depth=%d", got)
	}
}

type staticProbeRunner struct {
	result *checker.ServiceCheckResult
}

type sequenceProbeRunner struct {
	mu      sync.Mutex
	results []bool
	calls   int
}

type captureProbeRunner struct {
	check  checker.ServiceCheck
	result *checker.ServiceCheckResult
}

func (r *captureProbeRunner) CheckOne(_ context.Context, sc *checker.ServiceCheck, _ string) *checker.ServiceCheckResult {
	r.check = *sc
	r.check.HTTPHeaders = cloneStringMap(sc.HTTPHeaders)
	r.check.Config = cloneJSONMap(sc.Config)
	if r.result != nil {
		copy := *r.result
		return &copy
	}
	return &checker.ServiceCheckResult{CheckType: sc.CheckType, IsUp: true, Timestamp: time.Now().UTC()}
}

func (r *sequenceProbeRunner) CheckOne(context.Context, *checker.ServiceCheck, string) *checker.ServiceCheckResult {
	r.mu.Lock()
	defer r.mu.Unlock()
	index := r.calls
	r.calls++
	if index >= len(r.results) {
		index = len(r.results) - 1
	}
	return &checker.ServiceCheckResult{IsUp: r.results[index], Timestamp: time.Now().UTC()}
}

func TestServiceCheckHonorsConfiguredRetries(t *testing.T) {
	runner := &sequenceProbeRunner{results: []bool{false, true}}
	scheduler := newCheckScheduler(runner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	result := scheduler.runServiceWithRetries(context.Background(), &checker.ServiceCheck{RetryCount: 2, RetryDelay: time.Millisecond})
	if result == nil || !result.IsUp || runner.calls != 2 {
		t.Fatalf("retry result=%+v calls=%d, want success on second attempt", result, runner.calls)
	}
}

func TestRemoteServiceConfigMapsCheckerFields(t *testing.T) {
	const wire = `{
		"id":"154ca2ae-c7f6-4d21-bdda-d250bd9adbd4","name":"edge-http","check_type":"http",
		"target_host":"edge.example","target_port":8443,"target_url":"https://edge.example/health",
		"http_method":"POST","http_headers":{"X-ZenPlus":"edge"},"http_body":"{\"ready\":true}",
		"http_expected_status":204,"http_expected_statuses":"2xx","http_content_match":"ready",
		"http_follow_redirects":false,"http_ignore_tls_errors":true,"http_allow_insecure_auth":true,
		"config":{"record_type":"AAAA","count":4},"tls_warn_days":21,"tls_critical_days":5,
		"check_interval":45,"timeout":7,"retry_count":2,"retry_delay_s":17,"enabled":true
	}`
	var remote configServiceCheck
	if err := json.Unmarshal([]byte(wire), &remote); err != nil {
		t.Fatal(err)
	}
	runner := &captureProbeRunner{}
	scheduler := newCheckScheduler(runner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	scheduler.runService(context.Background(), remote, uuid.MustParse(remote.ID), 45*time.Second)
	got := runner.check
	if got.HTTPMethod != "POST" || got.HTTPHeaders["X-ZenPlus"] != "edge" || got.HTTPBody != `{"ready":true}` ||
		got.HTTPExpectedStatus != 204 || got.HTTPExpectedStatuses != "2xx" || !got.HTTPAllowInsecureAuth ||
		got.Config["record_type"] != "AAAA" || got.Config["count"] != float64(4) || got.RetryDelay != 17*time.Second ||
		got.HTTPFollowRedirects || !got.HTTPIgnoreTLSErrors {
		t.Fatalf("remote config was not mapped to checker fields: %+v", got)
	}

	defaults := &captureProbeRunner{}
	defaultScheduler := newCheckScheduler(defaults, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	remote.HTTPExpectedStatus = 0
	remote.HTTPExpectedStatuses = ""
	remote.RetryDelayS = 0
	defaultScheduler.runService(context.Background(), remote, uuid.MustParse(remote.ID), time.Minute)
	if defaults.check.HTTPExpectedStatus != http.StatusOK || defaults.check.RetryDelay != defaultServiceRetryDelay {
		t.Fatalf("service defaults = status %d delay %s", defaults.check.HTTPExpectedStatus, defaults.check.RetryDelay)
	}

	capped := &captureProbeRunner{}
	cappedScheduler := newCheckScheduler(capped, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	remote.RetryDelayS = 60 * 60
	cappedScheduler.runService(context.Background(), remote, uuid.MustParse(remote.ID), time.Minute)
	if capped.check.RetryDelay != maxServiceRetryDelay {
		t.Fatalf("retry delay was not capped: %s", capped.check.RetryDelay)
	}
}

func TestServiceRetryHonorsDelayAndContextCancellation(t *testing.T) {
	runner := &sequenceProbeRunner{results: []bool{false, true}}
	scheduler := newCheckScheduler(runner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	started := time.Now()
	result := scheduler.runServiceWithRetries(context.Background(), &checker.ServiceCheck{RetryCount: 2, RetryDelay: 25 * time.Millisecond})
	if elapsed := time.Since(started); result == nil || !result.IsUp || elapsed < 20*time.Millisecond || elapsed > time.Second {
		t.Fatalf("configured retry delay not honored: result=%+v elapsed=%s", result, elapsed)
	}

	cancelRunner := &sequenceProbeRunner{results: []bool{false, true}}
	cancelScheduler := newCheckScheduler(cancelRunner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(20 * time.Millisecond)
		cancel()
	}()
	started = time.Now()
	result = cancelScheduler.runServiceWithRetries(ctx, &checker.ServiceCheck{RetryCount: 2, RetryDelay: maxServiceRetryDelay})
	if elapsed := time.Since(started); result == nil || result.IsUp || cancelRunner.calls != 1 || elapsed > time.Second {
		t.Fatalf("retry wait did not honor cancellation: result=%+v calls=%d elapsed=%s", result, cancelRunner.calls, elapsed)
	}
}

func TestDeviceFailureGetsTwoConfirmationAttempts(t *testing.T) {
	runner := &sequenceProbeRunner{results: []bool{false, false, false}}
	scheduler := newCheckScheduler(runner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	result := scheduler.runWithRetries(context.Background(), &checker.ServiceCheck{CheckType: "icmp"}, 2)
	if result == nil || result.IsUp || runner.calls != 3 {
		t.Fatalf("confirmation result=%+v calls=%d, want down after 3 attempts", result, runner.calls)
	}
}

func TestDeviceResultPreservesICMPMetrics(t *testing.T) {
	result := &checker.ServiceCheckResult{
		CheckType: "icmp", IsUp: true, ResponseTime: 12 * time.Millisecond,
		PacketLoss: 0.25, Jitter: 2 * time.Millisecond, MinRTT: 9 * time.Millisecond, MaxRTT: 17 * time.Millisecond,
		PacketsSent: 4, PacketsReceived: 3, Timestamp: time.Now().UTC(),
	}
	runner := &captureProbeRunner{result: result}
	var payload map[string]any
	scheduler := newCheckScheduler(runner, "test", 1, func(_ string, item any) error {
		payload = item.(map[string]any)
		return nil
	}, zap.NewNop().Sugar())
	scheduler.runDevice(context.Background(), configDevice{
		ID: "device-1", Hostname: "router", IPAddress: "10.0.0.1", PingEnabled: true,
	}, time.Minute)
	if runner.check.Config["count"] != 3 || payload["packet_loss"] != 0.25 || payload["jitter_ms"] != float64(2) ||
		payload["min_rtt_ms"] != float64(9) || payload["max_rtt_ms"] != float64(17) ||
		payload["packets_sent"] != 4 || payload["packets_received"] != 3 {
		t.Fatalf("ICMP checker/payload metrics mismatch: check=%+v payload=%+v", runner.check, payload)
	}
}

func (r staticProbeRunner) CheckOne(context.Context, *checker.ServiceCheck, string) *checker.ServiceCheckResult {
	copy := *r.result
	return &copy
}

func TestServiceResultPreservesProtocolSpecificFields(t *testing.T) {
	days := 12
	valid := true
	matched := false
	expiry := time.Now().UTC().Add(12 * 24 * time.Hour)
	result := &checker.ServiceCheckResult{
		CheckType: "tls", IsUp: true, TLSDaysRemaining: &days, TLSValid: &valid, ContentMatched: &matched,
		TLSExpiry: &expiry, TLSIssuer: "Example CA", TLSSubject: "service.example",
		Timestamp: time.Now().UTC(),
	}
	var payload map[string]any
	scheduler := newCheckScheduler(staticProbeRunner{result: result}, "test", 1, func(_ string, item any) error {
		payload = item.(map[string]any)
		return nil
	}, zap.NewNop().Sugar())
	scheduler.runService(context.Background(), configServiceCheck{ID: uuid.NewString(), CheckType: "tls", Enabled: true}, uuid.New(), time.Minute)
	if payload["tls_days_remaining"] != result.TLSDaysRemaining || payload["tls_valid"] != result.TLSValid ||
		payload["tls_expiry_date"] != result.TLSExpiry || payload["tls_issuer"] != result.TLSIssuer || payload["tls_subject"] != result.TLSSubject ||
		payload["content_matched"] != result.ContentMatched {
		t.Fatalf("protocol-specific fields missing from payload: %+v", payload)
	}
	if _, exists := payload["status_code"]; exists {
		t.Fatalf("non-HTTP result contains invalid status_code: %+v", payload)
	}
}

func TestNonHTTPServiceResultPassesControllerShapedValidation(t *testing.T) {
	spool, err := sensorspool.Open(filepath.Join(t.TempDir(), "wal"), sensorspool.Options{
		MaxBytes: 1 << 20, MaxAge: time.Hour,
	})
	if err != nil {
		t.Fatal(err)
	}
	days := 30
	valid := true
	runner := staticProbeRunner{result: &checker.ServiceCheckResult{
		CheckType: "tls", IsUp: true, TLSDaysRemaining: &days, TLSValid: &valid,
		Timestamp: time.Now().UTC(),
	}}
	scheduler := newCheckScheduler(runner, "test", 1, newResultEnqueuer(spool), zap.NewNop().Sugar())
	scheduler.runService(context.Background(), configServiceCheck{
		ID: uuid.NewString(), CheckType: "tls", Enabled: true,
	}, uuid.New(), time.Minute)

	accepted := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != serviceResultsPath {
			http.Error(w, "wrong path", http.StatusNotFound)
			return
		}
		var body struct {
			Items []map[string]json.RawMessage `json:"items"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body.Items) != 1 {
			http.Error(w, "invalid result batch", http.StatusUnprocessableEntity)
			return
		}
		if raw, exists := body.Items[0]["status_code"]; exists {
			var code int
			if json.Unmarshal(raw, &code) != nil || code < 100 {
				http.Error(w, "status_code must be at least 100", http.StatusUnprocessableEntity)
				return
			}
		}
		accepted = true
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	c := &client{baseURL: server.URL, httpClient: server.Client(), state: &state{SensorID: "sensor", APIKey: "key"}}
	if _, err := drainSpool(context.Background(), time.Second, c, spool, zap.NewNop().Sugar()); err != nil {
		t.Fatal(err)
	}
	stats := spool.Stats()
	if !accepted || stats.Depth != 0 || stats.Dropped != 0 {
		t.Fatalf("controller validation accepted=%v stats=%+v", accepted, stats)
	}
}

func waitForStarts(t *testing.T, started <-chan string, count int) {
	t.Helper()
	for i := 0; i < count; i++ {
		select {
		case <-started:
		case <-time.After(time.Second):
			t.Fatalf("timed out waiting for probe start %d/%d", i+1, count)
		}
	}
}

func waitForWorkerCount(t *testing.T, scheduler *checkScheduler, want int) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if len(scheduler.sem) == want {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("active worker count=%d, want %d", len(scheduler.sem), want)
}

// Exercise the remote wire configuration through the production shared HTTP checker.
func TestRemoteAuthenticatedWorkflowUsesSharedChecker(t *testing.T) {
	calls := 0
	endpoint := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		user, password, ok := r.BasicAuth()
		if !ok || user != "operator" || password != "test-secret" {
			w.WriteHeader(401)
			return
		}
		calls++
		if r.URL.Path == "/login" {
			http.SetCookie(w, &http.Cookie{Name: "session", Value: "ready", Path: "/"})
			w.WriteHeader(204)
			return
		}
		cookie, err := r.Cookie("session")
		if err != nil || cookie.Value != "ready" {
			w.WriteHeader(403)
			return
		}
		fmt.Fprint(w, "healthy")
	}))
	defer endpoint.Close()
	credentialID := uuid.New()
	remote := configServiceCheck{ID: uuid.NewString(), CheckType: "http", Enabled: true,
		CredentialID: &credentialID, CredentialAuthType: "basic", CredentialUsername: "operator", CredentialSecret: "test-secret",
		HTTPIgnoreTLSErrors: true, Timeout: 2, WorkflowOperator: "all",
		WorkflowSteps: []checker.HTTPWorkflowStep{
			{Name: "Login", URL: endpoint.URL + "/login", Method: "GET", ExpectedStatuses: "204", Headers: map[string]string{"X-Test": "original"}},
			{Name: "Health", URL: endpoint.URL + "/health", Method: "GET", ExpectedStatuses: "200", ContentMatch: "healthy"},
		},
	}
	snapshot := cloneConfig(configResponse{ServiceChecks: []configServiceCheck{remote}})
	snapshot.ServiceChecks[0].WorkflowSteps[0].Headers["X-Test"] = "changed"
	if remote.WorkflowSteps[0].Headers["X-Test"] != "original" {
		t.Fatal("mutable workflow config leaked across snapshots")
	}
	var payload map[string]any
	scheduler := newCheckScheduler(checker.NewChecker(zap.NewNop().Sugar()), "site-a", 1, func(_ string, value any) error { payload = value.(map[string]any); return nil }, zap.NewNop().Sugar())
	scheduler.runService(context.Background(), remote, uuid.MustParse(remote.ID), time.Minute)
	if calls != 2 || payload["is_up"] != true {
		t.Fatalf("authenticated workflow failed: calls=%d payload=%v", calls, payload)
	}
	wire, _ := json.Marshal(payload)
	if strings.Contains(string(wire), "test-secret") {
		t.Fatal("credential leaked into telemetry")
	}
	remote.CredentialError = "Service credential could not be decrypted"
	scheduler.runService(context.Background(), remote, uuid.MustParse(remote.ID), time.Minute)
	if calls != 2 || payload["is_up"] != false {
		t.Fatal("credential error did not fail closed")
	}
}

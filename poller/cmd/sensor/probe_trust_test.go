package main

import (
	"context"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
	"go.uber.org/zap"
)

func TestSensorSchedulesAdministratorTrustAndClonesPolicy(t *testing.T) {
	disabled := false
	policy := &checker.ProbeTrustPolicy{AutoFetchIntermediates: &disabled, Certificates: []checker.ProbeTrustCertificate{{PEM: "test", Hosts: []string{"service.test"}}}}
	current := configResponse{ProbeTrust: policy, ServiceChecks: []configServiceCheck{{ID: uuid.NewString(), CheckType: "http", Enabled: true, Config: map[string]any{"probe_trust": "untrusted per-check value"}}}}
	snapshot := cloneConfig(current)
	snapshot.ProbeTrust.Certificates[0].Hosts[0] = "other.test"
	*snapshot.ProbeTrust.AutoFetchIntermediates = true
	if policy.Certificates[0].Hosts[0] != "service.test" || *policy.AutoFetchIntermediates {
		t.Fatal("snapshot changed original trust policy")
	}
	runner := &captureProbeRunner{}
	scheduler := newCheckScheduler(runner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	scheduler.jitter = func(string, time.Duration) time.Duration { return 0 }
	if scheduler.Schedule(context.Background(), current, time.Now()) != 1 {
		t.Fatal("probe not scheduled")
	}
	scheduler.workers.Wait()
	if runner.check.ProbeTrust != policy {
		t.Fatal("administrator trust policy did not reach the checker")
	}
}

func TestSensorAppliesManualHostAddressesFromItsConfiguration(t *testing.T) {
	current := configResponse{
		HostOverrides: []checker.HostOverrideEntry{{Host: "Portal.Example.Local", Address: "10.1.2.3"}},
		ServiceChecks: []configServiceCheck{{ID: uuid.NewString(), CheckType: "tcp", Enabled: true, TargetHost: "portal.example.local", TargetPort: 443}},
	}
	snapshot := cloneConfig(current)
	snapshot.HostOverrides[0].Address = "10.9.9.9"
	if current.HostOverrides[0].Address != "10.1.2.3" {
		t.Fatal("snapshot changed the original manual addresses")
	}
	runner := &captureProbeRunner{}
	scheduler := newCheckScheduler(runner, "test", 1, func(string, any) error { return nil }, zap.NewNop().Sugar())
	scheduler.jitter = func(string, time.Duration) time.Duration { return 0 }
	if scheduler.Schedule(context.Background(), current, time.Now()) != 1 {
		t.Fatal("probe not scheduled")
	}
	scheduler.workers.Wait()
	if address, ok := runner.check.HostOverrides.Lookup("portal.example.local"); !ok || address != "10.1.2.3" {
		t.Fatalf("manual host addresses did not reach the checker: %v", runner.check.HostOverrides)
	}
	// The controller learns from the heartbeat that this build applies them.
	found := false
	for _, capability := range sensorCapabilities {
		found = found || capability == "host_overrides"
	}
	if !found {
		t.Fatal("the sensor does not report the host_overrides capability")
	}
}

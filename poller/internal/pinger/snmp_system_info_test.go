package pinger

import (
	"context"
	"errors"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker/snmp"
	"go.uber.org/zap"
)

type systemInfoCall struct {
	deviceID    uuid.UUID
	sysObjectID string
	vendor      string
	model       string
	osVersion   string
	sysName     string
	sysDescr    string
}

// fakeSNMPLoader records UpsertSystemInfo calls, and the order of the
// identity-related writes (system info, interfaces, entities). The embedded
// interface is nil on purpose: handleSNMPResult for a system-only result
// must not reach any other loader method, and a call that does panics the
// test.
type fakeSNMPLoader struct {
	SNMPLoader

	mu     sync.Mutex
	calls  []systemInfoCall
	order  []string
	err    error
	entErr error
}

func (f *fakeSNMPLoader) UpsertEntities(ctx context.Context, deviceID uuid.UUID, ents []snmp.Entity) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.order = append(f.order, "UpsertEntities")
	return f.entErr
}

func (f *fakeSNMPLoader) UpsertInterfaces(ctx context.Context, deviceID uuid.UUID, ifs []snmp.Interface) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.order = append(f.order, "UpsertInterfaces")
	return nil
}

func (f *fakeSNMPLoader) UpsertSystemInfo(
	ctx context.Context, deviceID uuid.UUID,
	sysObjectID, vendor, model, osVersion, sysName, sysDescr string,
) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.order = append(f.order, "UpsertSystemInfo")
	f.calls = append(f.calls, systemInfoCall{
		deviceID: deviceID, sysObjectID: sysObjectID, vendor: vendor, model: model,
		osVersion: osVersion, sysName: sysName, sysDescr: sysDescr,
	})
	return f.err
}

func snmpTestEngine(loader SNMPLoader, profiles ...*snmp.Profile) *Engine {
	classifier := snmp.NewClassifier()
	classifier.SetProfiles(profiles)
	return &Engine{
		snmpLoader:     loader,
		snmpClassifier: classifier,
		logger:         zap.NewNop().Sugar(),
	}
}

func TestHandleSNMPResultPersistsSysDescrWithClassifiedFields(t *testing.T) {
	const descr = "FortiGate-60F v7.4.3,build2573,240115 (GA.M)"
	profile := &snmp.Profile{
		Name: "fortigate",
		Match: snmp.MatchRules{
			SysObjectIDPrefixes: []string{"1.3.6.1.4.1.12356"},
			ExtractModel:        `^(FortiGate-\w+)`,
			ExtractOSVersion:    `(v\d+\.\d+\.\d+)`,
			DefaultVendor:       "Fortinet",
		},
	}
	loader := &fakeSNMPLoader{}
	e := snmpTestEngine(loader, profile)
	dev := &snmp.Device{ID: uuid.New(), Hostname: "fw01"}

	e.handleSNMPResult(context.Background(), dev, &snmp.Result{System: &snmp.SystemInfo{
		SysDescr:    descr,
		SysObjectID: ".1.3.6.1.4.1.12356.101.1.600",
		SysName:     "fw01.example.net",
	}})

	if len(loader.calls) != 1 {
		t.Fatalf("UpsertSystemInfo called %d times, want 1", len(loader.calls))
	}
	got := loader.calls[0]
	want := systemInfoCall{
		deviceID:    dev.ID,
		sysObjectID: ".1.3.6.1.4.1.12356.101.1.600",
		vendor:      "Fortinet",
		model:       "FortiGate-60F",
		osVersion:   "v7.4.3",
		sysName:     "fw01.example.net",
		sysDescr:    descr,
	}
	if got != want {
		t.Fatalf("UpsertSystemInfo got %+v, want %+v", got, want)
	}
}

// The store is where sysDescr is sanitised; the engine passes the collected
// value through untouched, including when no profile matches.
func TestHandleSNMPResultPassesSysDescrWithoutProfile(t *testing.T) {
	loader := &fakeSNMPLoader{}
	e := snmpTestEngine(loader)

	e.handleSNMPResult(context.Background(), &snmp.Device{ID: uuid.New(), Hostname: "sw01"},
		&snmp.Result{System: &snmp.SystemInfo{SysDescr: "Some unknown agent 1.0"}})

	if len(loader.calls) != 1 {
		t.Fatalf("UpsertSystemInfo called %d times, want 1", len(loader.calls))
	}
	got := loader.calls[0]
	if got.sysDescr != "Some unknown agent 1.0" {
		t.Fatalf("sysDescr = %q, want the collected value", got.sysDescr)
	}
	if got.vendor != "" || got.model != "" || got.osVersion != "" {
		t.Fatalf("no profile matched but classified fields were set: %+v", got)
	}
}

// A device that answered the system GET but failed later in the cycle (for
// example the interface walk hit the per-device budget) still has a valid
// identity, so it is still persisted and stamped.
func TestHandleSNMPResultStampsIdentityOnPartialResult(t *testing.T) {
	loader := &fakeSNMPLoader{}
	e := snmpTestEngine(loader)

	e.handleSNMPResult(context.Background(), &snmp.Device{ID: uuid.New(), Hostname: "sw01"},
		&snmp.Result{
			Err:    errors.New("device budget 45s exceeded"),
			System: &snmp.SystemInfo{SysDescr: "Partial but identified"},
		})

	if len(loader.calls) != 1 || loader.calls[0].sysDescr != "Partial but identified" {
		t.Fatalf("partial result not persisted: %+v", loader.calls)
	}
}

// No system info means no successful identity poll: nothing is written, so
// identity_updated_at cannot advance for a device that did not answer.
func TestHandleSNMPResultSkipsSystemInfoWhenNotCollected(t *testing.T) {
	loader := &fakeSNMPLoader{}
	e := snmpTestEngine(loader)

	e.handleSNMPResult(context.Background(), &snmp.Device{ID: uuid.New(), Hostname: "sw01"},
		&snmp.Result{Err: errors.New("timeout")})

	if len(loader.calls) != 0 {
		t.Fatalf("UpsertSystemInfo called without system info: %+v", loader.calls)
	}
}

// A failed system-info write is logged and does not abort the rest of the
// writeback.
func TestHandleSNMPResultSurvivesUpsertSystemInfoError(t *testing.T) {
	loader := &fakeSNMPLoader{err: errors.New("db unavailable")}
	e := snmpTestEngine(loader)

	e.handleSNMPResult(context.Background(), &snmp.Device{ID: uuid.New(), Hostname: "sw01"},
		&snmp.Result{System: &snmp.SystemInfo{SysDescr: "x"}})

	if len(loader.calls) != 1 {
		t.Fatalf("UpsertSystemInfo called %d times, want 1", len(loader.calls))
	}
}

// The chassis row decides devices.model, so entities are written before the
// system info. In the other order a device's first poll stores the profile
// model for the length of the interface write and replaces it with the
// chassis model afterwards; a normaliser tick in that gap records a hardware
// change that never happened.
func TestHandleSNMPResultWritesEntitiesBeforeSystemInfo(t *testing.T) {
	loader := &fakeSNMPLoader{}
	e := snmpTestEngine(loader)

	e.handleSNMPResult(context.Background(), &snmp.Device{ID: uuid.New(), Hostname: "sw01"},
		&snmp.Result{
			System:     &snmp.SystemInfo{SysDescr: "Cisco IOS XE Software, Catalyst L3 Switch"},
			Interfaces: []snmp.Interface{{IfIndex: 1, IfName: "Gi1/0/1"}},
			Entities:   []snmp.Entity{{EntIndex: 1, Class: "chassis", ModelName: "C9300-48P"}},
		})

	want := []string{"UpsertEntities", "UpsertSystemInfo", "UpsertInterfaces"}
	if len(loader.order) != len(want) {
		t.Fatalf("write order = %v, want %v", loader.order, want)
	}
	for i := range want {
		if loader.order[i] != want[i] {
			t.Fatalf("write order = %v, want %v", loader.order, want)
		}
	}
}

// A failed entity write must not cost the device its identity writeback.
func TestHandleSNMPResultWritesSystemInfoWhenEntitiesFail(t *testing.T) {
	loader := &fakeSNMPLoader{entErr: errors.New("sync chassis identity: lock timeout")}
	e := snmpTestEngine(loader)

	e.handleSNMPResult(context.Background(), &snmp.Device{ID: uuid.New(), Hostname: "sw01"},
		&snmp.Result{
			System:   &snmp.SystemInfo{SysDescr: "Cisco IOS XE Software"},
			Entities: []snmp.Entity{{EntIndex: 1, Class: "chassis", ModelName: "C9300-48P"}},
		})

	if len(loader.calls) != 1 || loader.calls[0].sysDescr != "Cisco IOS XE Software" {
		t.Fatalf("system info not written after an entity failure: %+v", loader.calls)
	}
}

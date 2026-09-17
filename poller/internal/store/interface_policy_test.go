package store

import (
	"github.com/zenplus/poller/internal/checker/snmp"
	"testing"
	"time"
)

func TestInterfacePolicySurvivesIndexSwapWithoutLeakingToNewPort(t *testing.T) {
	speed := int64(100000000)
	old := []interfacePolicy{{1, "uplink", false, &speed, time.Time{}}, {2, "access", true, nil, time.Time{}}}
	current := []snmp.Interface{{IfIndex: 2, IfName: "uplink"}, {IfIndex: 1, IfName: "access"}, {IfIndex: 3, IfName: "new"}}
	got := reconcileInterfacePolicy(old, current)
	if got[2].Monitored || got[2].Speed == nil || *got[2].Speed != speed {
		t.Fatal("uplink policy lost")
	}
	if !got[1].Monitored || got[1].Speed != nil || !got[3].Monitored {
		t.Fatal("policy applied to wrong port")
	}
}
func TestAmbiguousNamesDoNotMovePolicyAcrossPorts(t *testing.T) {
	old := []interfacePolicy{{1, "duplicate", false, nil, time.Time{}}, {2, "duplicate", true, nil, time.Time{}}}
	got := reconcileInterfacePolicy(old, []snmp.Interface{{IfIndex: 3, IfName: "duplicate"}})
	if !got[3].Monitored {
		t.Fatal("ambiguous name inherited unrelated policy")
	}
}

func TestSuccessiveReindexUsesMostRecentPolicy(t *testing.T) {
	old := []interfacePolicy{{Index: 1, Name: "uplink", Monitored: true, LastSeen: time.Unix(100, 0)},
		{Index: 2, Name: "uplink", Monitored: false, LastSeen: time.Unix(200, 0)}}
	got := reconcileInterfacePolicy(old, []snmp.Interface{{IfIndex: 3, IfName: "uplink"}})
	if got[3].Monitored {
		t.Fatal("stale historical index overrode latest operator selection")
	}
}

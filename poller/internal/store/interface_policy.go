package store

import (
	"github.com/zenplus/poller/internal/checker/snmp"
	"time"
)

type interfacePolicy struct {
	Index     int
	Name      string
	Monitored bool
	Speed     *int64
	LastSeen  time.Time
}

// Preserve operator policy by an unambiguous logical port name when ifIndex
// changes. Never copy policy by a reused numeric index onto a different port.
func reconcileInterfacePolicy(old []interfacePolicy, current []snmp.Interface) map[int]interfacePolicy {
	names := map[string][]interfacePolicy{}
	counts := map[string]int{}
	for _, p := range old {
		if p.Name != "" {
			candidates := names[p.Name]
			if len(candidates) == 0 || p.LastSeen.After(candidates[0].LastSeen) {
				names[p.Name] = []interfacePolicy{p}
			} else if p.LastSeen.Equal(candidates[0].LastSeen) {
				names[p.Name] = append(candidates, p)
			}
		}
	}
	for _, i := range current {
		counts[i.IfName]++
	}
	out := map[int]interfacePolicy{}
	for _, i := range current {
		p := interfacePolicy{Index: i.IfIndex, Name: i.IfName, Monitored: true}
		if candidates := names[i.IfName]; len(candidates) == 1 && counts[i.IfName] == 1 {
			p.Monitored = candidates[0].Monitored
			p.Speed = candidates[0].Speed
		} else {
			for _, previous := range old {
				if previous.Index == i.IfIndex && previous.Name == i.IfName {
					p.Monitored = previous.Monitored
					p.Speed = previous.Speed
					break
				}
			}
		}
		out[i.IfIndex] = p
	}
	return out
}

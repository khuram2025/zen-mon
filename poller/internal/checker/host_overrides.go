package checker

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/netip"
	"strings"
	"sync"
)

// Manual host addresses (docs/service-check-connections.md). An administrator
// maps a host name to the address a service check connects to. Only the
// dialled address changes: the URL, the Host header, the TLS server name and
// the certificate name check keep the name, as with `curl --resolve`.
// DNS checks never read these: they test name resolution itself.

// HostOverrideScopeCentral is the place name of the central appliance.
const HostOverrideScopeCentral = "central"

// HostOverrideEntry is one row of system_settings['monitoring.host_overrides'].
// Sensors receive rows already selected for them, with the scope left empty.
type HostOverrideEntry struct {
	Host     string `json:"host"`
	Address  string `json:"address"`
	Scope    string `json:"scope,omitempty"`     // "all" (or empty), "central", "sensor"
	SensorID string `json:"sensor_id,omitempty"` // set when Scope is "sensor"
}

// HostOverrides is the table for one place: normalised name to IP literal.
// It is built once per configuration load and only read afterwards.
type HostOverrides map[string]string

// ParseHostOverrideEntries reads the stored document ({"entries": [...]}).
// It never fails: a row, or a whole document, that is not what the API writes
// is left out, so a damaged setting can cost its own manual addresses but
// never stop service checks from loading.
func ParseHostOverrideEntries(raw []byte) []HostOverrideEntry {
	var doc struct {
		Entries []json.RawMessage `json:"entries"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil
	}
	entries := make([]HostOverrideEntry, 0, len(doc.Entries))
	for _, row := range doc.Entries {
		var entry HostOverrideEntry
		if err := json.Unmarshal(row, &entry); err == nil {
			entries = append(entries, entry)
		}
	}
	return entries
}

func normalizeOverrideHost(host string) string {
	return strings.ToLower(strings.TrimSuffix(strings.TrimSpace(host), "."))
}

// ResolveHostOverrides selects the rows that apply to one place ("central" or
// a sensor id). A row for that place wins over an "everywhere" row for the
// same name. Rows that are not a name and an IP address are skipped, so a
// damaged row can never redirect a check to a name of its own.
func ResolveHostOverrides(entries []HostOverrideEntry, place string) HostOverrides {
	out := HostOverrides{}
	specific := map[string]bool{}
	for _, entry := range entries {
		host := normalizeOverrideHost(entry.Host)
		addr, err := netip.ParseAddr(strings.TrimSpace(entry.Address))
		if host == "" || err != nil || addr.Zone() != "" {
			continue
		}
		if _, notName := netip.ParseAddr(host); notName == nil {
			continue // an address, not a name
		}
		address := addr.Unmap().String()
		switch entry.Scope {
		case "", "all":
			if !specific[host] {
				out[host] = address
			}
		case HostOverrideScopeCentral:
			if place == HostOverrideScopeCentral {
				out[host] = address
				specific[host] = true
			}
		case "sensor":
			if place != "" && place != HostOverrideScopeCentral && strings.EqualFold(entry.SensorID, place) {
				out[host] = address
				specific[host] = true
			}
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// Lookup returns the manual address for a host name. IP literals never match.
func (o HostOverrides) Lookup(host string) (string, bool) {
	if len(o) == 0 {
		return "", false
	}
	address, ok := o[normalizeOverrideHost(host)]
	return address, ok
}

// connectAddress turns "name:port" into the address to dial. `used` is the
// manual address when one applied. ipVersion ("ipv4", "ipv6" or anything else
// for no restriction) refuses an address of the other family in plain words
// instead of a resolver error.
func (o HostOverrides) connectAddress(addr, ipVersion string) (target, host, used string, err error) {
	name, port, splitErr := net.SplitHostPort(addr)
	if splitErr != nil {
		return addr, "", "", nil
	}
	address, ok := o.Lookup(name)
	if !ok {
		return addr, name, "", nil
	}
	parsed, parseErr := netip.ParseAddr(address)
	if parseErr != nil {
		return addr, name, "", nil
	}
	if ipVersion == "ipv4" && !parsed.Is4() {
		return "", name, "", fmt.Errorf("the manual address %s for %s is IPv6, but this check is set to IPv4 only", address, name)
	}
	if ipVersion == "ipv6" && !parsed.Is6() {
		return "", name, "", fmt.Errorf("the manual address %s for %s is IPv4, but this check is set to IPv6 only", address, name)
	}
	return net.JoinHostPort(address, port), name, address, nil
}

// overrideNote collects the first manual address a probe used, so the result
// can say the check did not go through DNS.
type overrideNote struct {
	mu      sync.Mutex
	host    string
	address string
}

type overrideNoteKey struct{}

func withOverrideNote(ctx context.Context) (context.Context, *overrideNote) {
	note := &overrideNote{}
	return context.WithValue(ctx, overrideNoteKey{}, note), note
}

func noteOverride(ctx context.Context, host, address string) {
	note, _ := ctx.Value(overrideNoteKey{}).(*overrideNote)
	if note == nil || address == "" {
		return
	}
	note.mu.Lock()
	if note.address == "" {
		note.host, note.address = normalizeOverrideHost(host), address
	}
	note.mu.Unlock()
}

func (n *overrideNote) get() (host, address string) {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.host, n.address
}

// overrideDiagnostics marks a TCP, TLS or ICMP result that used a manual
// address. HTTP results carry the same two fields through traceProbe.
func overrideDiagnostics(sc *ServiceCheck, host, address string, connected bool) *NetworkDiagnostics {
	d := &NetworkDiagnostics{IPVersion: probeIPVersion(sc), OverrideHost: normalizeOverrideHost(host), OverrideAddress: address}
	if connected {
		d.RemoteIP = address
	}
	return d
}

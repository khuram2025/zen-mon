package snmp

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"sync"
	"time"

	"github.com/google/uuid"
	g "github.com/gosnmp/gosnmp"
)

// TrapRecord is one decoded SNMP trap bound for storage + alerting.
type TrapRecord struct {
	DeviceID  *uuid.UUID // nil if source IP isn't in devices
	SourceIP  net.IP
	TrapOID   string
	TrapName  string // human label if known; otherwise same as OID
	Severity  string // "info" | "warning" | "critical" (best-effort)
	Message   string
	Bindings  string // JSON of varbinds
	Timestamp time.Time
	PollerID  string
}

// TrapSink persists decoded traps.
type TrapSink interface {
	WriteTrap(t TrapRecord)
}

// DeviceLookup resolves a source IP to a device UUID. Returns
// (uuid.Nil, false) when not found.
type DeviceLookup interface {
	LookupDeviceByIP(ctx context.Context, ip net.IP) (uuid.UUID, bool)
}

// TrapListener receives SNMPv1/v2c traps on UDP/162. v3 traps are
// deferred to a later phase (USM engine discovery is involved).
type TrapListener struct {
	pollerID string
	bind     string
	sink     TrapSink
	lookup   DeviceLookup
	logger   interface {
		Infof(string, ...any)
		Warnf(string, ...any)
		Errorf(string, ...any)
	}

	listener *g.TrapListener
	mu       sync.Mutex
	running  bool
}

// NewTrapListener constructs a listener that will bind to `bind`
// (e.g. "0.0.0.0:162") when Start is called. `community` is the
// expected v2c community — gosnmp does NOT enforce it, so we filter
// in the handler if non-empty.
func NewTrapListener(
	pollerID, bind string,
	sink TrapSink,
	lookup DeviceLookup,
	logger interface {
		Infof(string, ...any)
		Warnf(string, ...any)
		Errorf(string, ...any)
	},
) *TrapListener {
	return &TrapListener{
		pollerID: pollerID,
		bind:     bind,
		sink:     sink,
		lookup:   lookup,
		logger:   logger,
	}
}

// Start binds UDP/162 and begins dispatching traps. Blocks until the
// listener is up, then returns — the actual packet loop runs in a
// background goroutine managed by gosnmp.
func (t *TrapListener) Start(ctx context.Context) error {
	t.mu.Lock()
	if t.running {
		t.mu.Unlock()
		return nil
	}
	t.listener = g.NewTrapListener()
	t.listener.OnNewTrap = t.onTrap
	t.listener.Params = g.Default
	// gosnmp defaults to v2c community "public"; we accept any.
	t.listener.Params.Community = ""
	t.running = true
	t.mu.Unlock()

	// gosnmp.Listen is blocking, so run it in a goroutine.
	errCh := make(chan error, 1)
	go func() {
		t.logger.Infof("SNMP trap listener starting on %s", t.bind)
		err := t.listener.Listen(t.bind)
		if err != nil {
			errCh <- err
		}
	}()

	// Let the bind error surface for a moment before declaring success.
	select {
	case err := <-errCh:
		t.mu.Lock()
		t.running = false
		t.mu.Unlock()
		return fmt.Errorf("trap listener bind %s: %w", t.bind, err)
	case <-time.After(200 * time.Millisecond):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Close stops the listener.
func (t *TrapListener) Close() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.running {
		return
	}
	if t.listener != nil {
		t.listener.Close()
	}
	t.running = false
}

// onTrap is invoked by gosnmp for every received PDU. It decodes the
// varbinds, matches the source to a device, and hands off to the sink.
func (t *TrapListener) onTrap(packet *g.SnmpPacket, addr *net.UDPAddr) {
	if packet == nil || addr == nil {
		return
	}

	rec := TrapRecord{
		SourceIP:  addr.IP,
		Timestamp: time.Now().UTC(),
		PollerID:  t.pollerID,
	}

	// Pull trap-OID out of the varbinds. For v2c traps it is the
	// value bound to 1.3.6.1.6.3.1.1.4.1.0 (snmpTrapOID.0).
	type bind struct {
		OID   string `json:"oid"`
		Type  string `json:"type"`
		Value any    `json:"value"`
	}
	binds := make([]bind, 0, len(packet.Variables))
	for _, v := range packet.Variables {
		binds = append(binds, bind{
			OID:   v.Name,
			Type:  v.Type.String(),
			Value: stringifyPDUValue(v),
		})
		if v.Name == ".1.3.6.1.6.3.1.1.4.1.0" || v.Name == "1.3.6.1.6.3.1.1.4.1.0" {
			rec.TrapOID = fmt.Sprint(v.Value)
		}
	}

	// v1 traps carry the enterprise OID + generic-trap number in
	// dedicated header fields rather than a varbind.
	if rec.TrapOID == "" && packet.Enterprise != "" {
		rec.TrapOID = fmt.Sprintf("%s.%d.%d",
			packet.Enterprise, packet.GenericTrap, packet.SpecificTrap)
	}
	if rec.TrapOID == "" {
		rec.TrapOID = "unknown"
	}
	rec.TrapName = rec.TrapOID

	if data, err := json.Marshal(binds); err == nil {
		rec.Bindings = string(data)
	} else {
		rec.Bindings = "[]"
	}

	rec.Severity = severityFromTrapOID(rec.TrapOID)
	rec.Message = fmt.Sprintf("trap %s from %s", rec.TrapOID, addr.IP)

	if t.lookup != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		if id, ok := t.lookup.LookupDeviceByIP(ctx, addr.IP); ok {
			rec.DeviceID = &id
		}
	}

	if t.sink != nil {
		t.sink.WriteTrap(rec)
	}
	t.logger.Infof("trap received: src=%s oid=%s severity=%s device=%v",
		addr.IP, rec.TrapOID, rec.Severity, rec.DeviceID)
}

// severityFromTrapOID is a best-effort mapping for well-known trap
// OIDs. Anything unrecognised defaults to "info".
func severityFromTrapOID(oid string) string {
	switch oid {
	case ".1.3.6.1.6.3.1.1.5.1", "1.3.6.1.6.3.1.1.5.1": // coldStart
		return "warning"
	case ".1.3.6.1.6.3.1.1.5.2", "1.3.6.1.6.3.1.1.5.2": // warmStart
		return "info"
	case ".1.3.6.1.6.3.1.1.5.3", "1.3.6.1.6.3.1.1.5.3": // linkDown
		return "critical"
	case ".1.3.6.1.6.3.1.1.5.4", "1.3.6.1.6.3.1.1.5.4": // linkUp
		return "info"
	case ".1.3.6.1.6.3.1.1.5.5", "1.3.6.1.6.3.1.1.5.5": // authFailure
		return "warning"
	}
	return "info"
}

// stringifyPDUValue returns a JSON-safe representation of a varbind
// value. Strings and integers pass through; octet strings become
// regular strings when printable, hex otherwise.
func stringifyPDUValue(v g.SnmpPDU) any {
	switch x := v.Value.(type) {
	case []byte:
		// Try UTF-8; fall back to hex.
		for _, b := range x {
			if b < 0x20 && b != '\t' && b != '\n' && b != '\r' {
				return fmt.Sprintf("%x", x)
			}
		}
		return string(x)
	case string, int, int32, int64, uint, uint32, uint64, bool, nil:
		return x
	default:
		return fmt.Sprint(x)
	}
}

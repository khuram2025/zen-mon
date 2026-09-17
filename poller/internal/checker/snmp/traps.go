package snmp

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	g "github.com/gosnmp/gosnmp"
)

// TrapRecord is one decoded SNMP trap bound for storage + alerting.
type TrapRecord struct {
	EventID   uuid.UUID  // retained when the decoded event is replayed to the API
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

// TrapListener receives v1/v2c and configured-source v3 USM traps.
// Credentials refresh with the device inventory; v3 informs require a separate authoritative engine.
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

	listener *net.UDPConn
	v3       map[string]*g.GoSNMP
	mu       sync.Mutex
	running  bool
}

// NewTrapListener constructs a listener that will bind to `bind`
// (e.g. "0.0.0.0:162") when Start is called.
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

// SetV3Devices replaces the accepted source/credential map atomically.
func (t *TrapListener) SetV3Devices(devices []*Device) {
	next := make(map[string]*g.GoSNMP)
	for _, d := range devices {
		if d.Version != "3" {
			continue
		}
		params, err := NewSession(d)
		if err != nil {
			t.logger.Warnf("SNMPv3 trap configuration invalid for device %s", d.ID)
			continue
		}
		next[d.IPAddress.String()] = params
	}
	t.mu.Lock()
	t.v3 = next
	t.mu.Unlock()
}

// Start binds synchronously, so success proves the listener owns its socket.
func (t *TrapListener) Start(ctx context.Context) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.running {
		return nil
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	addr, err := net.ResolveUDPAddr("udp", t.bind)
	if err != nil {
		return err
	}
	conn, err := net.ListenUDP("udp", addr)
	if err != nil {
		return err
	}
	t.listener = conn
	t.running = true
	go t.receive(ctx, conn)
	return nil
}

func (t *TrapListener) receive(ctx context.Context, conn *net.UDPConn) {
	defer func() {
		_ = conn.Close()
		t.mu.Lock()
		if t.listener == conn {
			t.running = false
		}
		t.mu.Unlock()
	}()
	buffer := make([]byte, 65535)
	for {
		_ = conn.SetReadDeadline(time.Now().Add(time.Second))
		n, addr, err := conn.ReadFromUDP(buffer)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			if e, ok := err.(net.Error); ok && e.Timeout() {
				continue
			}
			return
		}
		if ctx.Err() != nil {
			return
		}
		t.mu.Lock()
		configured := t.v3[addr.IP.String()]
		t.mu.Unlock()
		params := *g.Default
		// An empty table rejects unknown v3 senders before any decoding fallback.
		params.TrapSecurityParametersTable = g.NewSnmpV3SecurityParametersTable(g.Logger{})
		if configured != nil {
			params = *configured
			params.SecurityParameters = configured.SecurityParameters.Copy()
		}
		packet, err := params.UnmarshalTrap(buffer[:n], true)
		if err != nil {
			continue
		}
		if packet.Version == g.Version3 {
			if configured == nil || packet.MsgFlags&3 != configured.MsgFlags&3 || packet.PDUType == g.InformRequest {
				continue
			}
			received, ok := packet.SecurityParameters.(*g.UsmSecurityParameters)
			expected := configured.SecurityParameters.(*g.UsmSecurityParameters)
			if !ok || received.UserName != expected.UserName {
				continue
			}
		}
		if packet.PDUType != g.Trap && packet.PDUType != g.SNMPv2Trap && packet.PDUType != g.InformRequest {
			continue
		}
		t.onTrap(packet, addr)
		if packet.PDUType == g.InformRequest {
			packet.PDUType = g.GetResponse
			if response, err := packet.MarshalMsg(); err == nil {
				_, _ = conn.WriteToUDP(response, addr)
			}
		}
	}
}

func (t *TrapListener) Close() {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.listener != nil {
		_ = t.listener.Close()
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
		EventID:   uuid.New(),
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
		// RFC 3584: standard generic traps map to snmpTraps.(n+1);
		// enterpriseSpecific maps to enterprise.0.specific, not enterprise.6.specific.
		if packet.GenericTrap >= 0 && packet.GenericTrap < 6 {
			rec.TrapOID = fmt.Sprintf("1.3.6.1.6.3.1.1.5.%d", packet.GenericTrap+1)
		} else {
			rec.TrapOID = fmt.Sprintf("%s.0.%d", strings.Trim(packet.Enterprise, "."), packet.SpecificTrap)
		}
	}
	if rec.TrapOID == "" {
		rec.TrapOID = "unknown"
	}
	rec.TrapOID = strings.TrimPrefix(rec.TrapOID, ".")
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

package snmp

import (
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	g "github.com/gosnmp/gosnmp"
)

// NewSession builds a *gosnmp.GoSNMP configured for the given Device.
// The returned client is NOT connected — call Connect() at use time.
// Callers should Close() when done.
func NewSession(d *Device) (*g.GoSNMP, error) {
	if d.IPAddress == nil {
		return nil, fmt.Errorf("device %s: no IP address", d.Hostname)
	}
	port := uint16(161)
	if d.Port > 0 {
		port = uint16(d.Port)
	}

	timeout := 2 * time.Second
	if d.TimeoutMs > 0 {
		timeout = time.Duration(d.TimeoutMs) * time.Millisecond
	}
	retries := 2
	if d.Retries > 0 {
		retries = d.Retries
	}
	maxReps := uint8(25)
	if d.MaxRepetitions > 0 && d.MaxRepetitions < 256 {
		maxReps = uint8(d.MaxRepetitions)
	}

	s := &g.GoSNMP{
		Target:         d.IPAddress.String(),
		Port:           port,
		Transport:      "udp",
		Timeout:        timeout,
		Retries:        retries,
		MaxRepetitions: uint32(maxReps),
		// ExponentialTimeout is NOT set: with it enabled a 2s timeout
		// grows to 2+4+8=14s per call, which compounds to minutes for
		// a full poll of an unreachable device. A flat timeout is fine
		// for Phase 1.
	}

	switch d.Version {
	case "1":
		s.Version = g.Version1
		s.Community = d.Community
	case "2c", "": // default
		s.Version = g.Version2c
		s.Community = d.Community
	case "3":
		s.Version = g.Version3
		s.SecurityModel = g.UserSecurityModel
		s.MsgFlags = v3Flags(d)
		sp, err := v3SecurityParams(d)
		if err != nil {
			return nil, fmt.Errorf("v3 params for %s: %w", d.Hostname, err)
		}
		s.SecurityParameters = sp
		if d.V3Context != "" {
			s.ContextName = d.V3Context
		}
	default:
		return nil, fmt.Errorf("unsupported SNMP version %q", d.Version)
	}

	return s, nil
}

func v3Flags(d *Device) g.SnmpV3MsgFlags {
	hasAuth := d.AuthProtocol != "" && d.AuthPassphrase != ""
	hasPriv := d.PrivProtocol != "" && d.PrivPassphrase != ""
	switch {
	case hasAuth && hasPriv:
		return g.AuthPriv
	case hasAuth:
		return g.AuthNoPriv
	default:
		return g.NoAuthNoPriv
	}
}

func v3SecurityParams(d *Device) (*g.UsmSecurityParameters, error) {
	authProto, err := parseAuthProtocol(d.AuthProtocol)
	if err != nil {
		return nil, err
	}
	privProto, err := parsePrivProtocol(d.PrivProtocol)
	if err != nil {
		return nil, err
	}
	return &g.UsmSecurityParameters{
		UserName:                 d.V3Username,
		AuthenticationProtocol:   authProto,
		AuthenticationPassphrase: d.AuthPassphrase,
		PrivacyProtocol:          privProto,
		PrivacyPassphrase:        d.PrivPassphrase,
	}, nil
}

func parseAuthProtocol(s string) (g.SnmpV3AuthProtocol, error) {
	switch strings.ToUpper(s) {
	case "", "NONE":
		return g.NoAuth, nil
	case "MD5":
		return g.MD5, nil
	case "SHA", "SHA1":
		return g.SHA, nil
	case "SHA224":
		return g.SHA224, nil
	case "SHA256":
		return g.SHA256, nil
	case "SHA384":
		return g.SHA384, nil
	case "SHA512":
		return g.SHA512, nil
	}
	return g.NoAuth, fmt.Errorf("unknown auth protocol %q", s)
}

func parsePrivProtocol(s string) (g.SnmpV3PrivProtocol, error) {
	switch strings.ToUpper(s) {
	case "", "NONE":
		return g.NoPriv, nil
	case "DES":
		return g.DES, nil
	case "AES", "AES128":
		return g.AES, nil
	case "AES192":
		return g.AES192, nil
	case "AES256":
		return g.AES256, nil
	}
	return g.NoPriv, fmt.Errorf("unknown priv protocol %q", s)
}

// SessionCache keeps one open *gosnmp.GoSNMP per device. Sessions
// are rebuilt on credential change or after N consecutive failures.
type SessionCache struct {
	mu       sync.Mutex
	sessions map[uuid.UUID]*cachedSession
}

type cachedSession struct {
	client   *g.GoSNMP
	key      string // fingerprint of Device SNMP fields
	failures int
}

func NewSessionCache() *SessionCache {
	return &SessionCache{sessions: make(map[uuid.UUID]*cachedSession)}
}

// Acquire returns a connected session for the device. The returned
// session should be returned via Release — it is shared across
// collectors but not across goroutines; callers must serialize.
func (c *SessionCache) Acquire(d *Device) (*g.GoSNMP, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	key := fingerprint(d)
	entry, ok := c.sessions[d.ID]
	if ok && entry.key == key && entry.client != nil && entry.client.Conn != nil {
		return entry.client, nil
	}

	// (Re)build and connect.
	if ok && entry.client != nil && entry.client.Conn != nil {
		_ = entry.client.Conn.Close()
	}
	s, err := NewSession(d)
	if err != nil {
		return nil, err
	}
	if err := s.Connect(); err != nil {
		return nil, fmt.Errorf("connect %s: %w", d.Hostname, err)
	}
	c.sessions[d.ID] = &cachedSession{client: s, key: key}
	return s, nil
}

// MarkFailure increments the failure counter for a device and
// evicts the session after 3 strikes so the next poll rebuilds it.
func (c *SessionCache) MarkFailure(id uuid.UUID) {
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.sessions[id]
	if !ok {
		return
	}
	entry.failures++
	if entry.failures >= 3 && entry.client != nil && entry.client.Conn != nil {
		_ = entry.client.Conn.Close()
		delete(c.sessions, id)
	}
}

// MarkSuccess resets the failure counter.
func (c *SessionCache) MarkSuccess(id uuid.UUID) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if entry, ok := c.sessions[id]; ok {
		entry.failures = 0
	}
}

// Drop forcibly evicts a session (e.g. on device deletion).
func (c *SessionCache) Drop(id uuid.UUID) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if entry, ok := c.sessions[id]; ok {
		if entry.client != nil && entry.client.Conn != nil {
			_ = entry.client.Conn.Close()
		}
		delete(c.sessions, id)
	}
}

// Close evicts all sessions. Used on shutdown.
func (c *SessionCache) Close() {
	c.mu.Lock()
	defer c.mu.Unlock()
	for id, entry := range c.sessions {
		if entry.client != nil && entry.client.Conn != nil {
			_ = entry.client.Conn.Close()
		}
		delete(c.sessions, id)
	}
}

func fingerprint(d *Device) string {
	// Any field that materially changes the on-wire session.
	return strings.Join([]string{
		d.IPAddress.String(),
		fmt.Sprint(d.Port),
		d.Version,
		d.Community,
		d.V3Username,
		d.V3Context,
		d.AuthProtocol,
		d.PrivProtocol,
		d.AuthPassphrase, // cheap to include; cache is in-memory only
		d.PrivPassphrase,
	}, "|")
}

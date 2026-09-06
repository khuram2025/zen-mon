package main

import (
	"context"
	"fmt"
	"net"
	"regexp"
	"time"

	g "github.com/gosnmp/gosnmp"
	snmpchecker "github.com/zenplus/poller/internal/checker/snmp"
)

type snmpConfig struct {
	Version        string   `json:"version"`
	Port           int      `json:"port"`
	Community      string   `json:"community"`
	Username       string   `json:"v3_username"`
	Context        string   `json:"v3_context"`
	AuthProtocol   string   `json:"v3_auth_protocol"`
	AuthPassphrase string   `json:"v3_auth_passphrase"`
	PrivProtocol   string   `json:"v3_priv_protocol"`
	PrivPassphrase string   `json:"v3_priv_passphrase"`
	TimeoutMS      int      `json:"timeout_ms"`
	Retries        int      `json:"retries"`
	Interval       int      `json:"interval"`
	OIDs           []string `json:"oids"`
}

var scalarOID = regexp.MustCompile(`^\.?[0-2](\.[0-9]+)+$`)

func snmpSession(d configDevice) (*g.GoSNMP, error) {
	cfg := d.SNMP
	if cfg == nil || len(cfg.OIDs) == 0 || len(cfg.OIDs) > 32 {
		return nil, fmt.Errorf("SNMP requires 1 to 32 scalar OIDs")
	}
	for _, oid := range cfg.OIDs {
		if !scalarOID.MatchString(oid) {
			return nil, fmt.Errorf("invalid scalar OID")
		}
	}
	if cfg.Port < 1 || cfg.Port > 65535 || cfg.TimeoutMS < 100 || cfg.TimeoutMS > 10000 || cfg.Retries < 0 || cfg.Retries > 3 {
		return nil, fmt.Errorf("SNMP transport settings out of range")
	}
	if cfg.Version != "3" && cfg.Community == "" {
		return nil, fmt.Errorf("SNMP community is required")
	}
	if cfg.Version == "3" && cfg.Username == "" {
		return nil, fmt.Errorf("SNMPv3 username is required")
	}
	if cfg.PrivPassphrase != "" && cfg.AuthPassphrase == "" {
		return nil, fmt.Errorf("SNMP privacy requires authentication")
	}
	if (cfg.AuthProtocol != "" && cfg.AuthProtocol != "NONE") != (cfg.AuthPassphrase != "") ||
		(cfg.PrivProtocol != "" && cfg.PrivProtocol != "NONE") != (cfg.PrivPassphrase != "") {
		return nil, fmt.Errorf("SNMP security protocol and passphrase must be configured together")
	}
	session, err := snmpchecker.NewSession(&snmpchecker.Device{
		Hostname: d.Hostname, IPAddress: net.ParseIP(d.IPAddress), Version: cfg.Version,
		Port: cfg.Port, Community: cfg.Community, V3Username: cfg.Username, V3Context: cfg.Context,
		AuthProtocol: cfg.AuthProtocol, AuthPassphrase: cfg.AuthPassphrase,
		PrivProtocol: cfg.PrivProtocol, PrivPassphrase: cfg.PrivPassphrase, TimeoutMs: cfg.TimeoutMS,
	})
	if err != nil {
		return nil, err
	}
	session.Retries = cfg.Retries
	return session, nil
}

func numericSNMP(pdu g.SnmpPDU) (float64, bool) {
	switch pdu.Type {
	case g.Integer, g.Counter32, g.Gauge32, g.TimeTicks, g.Counter64, g.Uinteger32:
		value, _ := g.ToBigInt(pdu.Value).Float64()
		return value, true
	}
	return 0, false
}

func (s *checkScheduler) runSNMP(ctx context.Context, d configDevice) {
	session, err := snmpSession(d)
	if err != nil {
		s.logger.Warnf("SNMP configuration invalid for device %s: %v", d.ID, err)
		return
	}
	probeCtx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	session.Context = probeCtx
	if err = session.Connect(); err != nil {
		s.logger.Warnf("SNMP connect failed for device %s", d.ID)
		return
	}
	defer session.Conn.Close()
	packet, err := session.Get(d.SNMP.OIDs)
	if err != nil || packet == nil || packet.Error != g.NoError {
		s.logger.Warnf("SNMP GET failed for device %s", d.ID)
		return
	}
	now := time.Now().UTC()
	for _, pdu := range packet.Variables {
		value, ok := numericSNMP(pdu)
		if !ok {
			continue
		}
		unit := ""
		if pdu.Type == g.TimeTicks {
			value /= 100
			unit = "seconds"
		}
		if err := s.enqueue("/api/v1/sensor/results/snmp", map[string]any{"device_id": d.ID, "timestamp": now, "oid": pdu.Name, "value": value, "unit": unit}); err != nil {
			s.logger.Errorf("SNMP result spool failed for device %s: %v", d.ID, err)
		}
	}
}

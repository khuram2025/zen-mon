// Package snmp implements SNMP v1/v2c/v3 polling for ZenPlus.
//
// Layout:
//
//	types.go     — domain types (Device, Interface, Entity, Sensor, Result)
//	oids.go      — standard MIB OID constants
//	crypto.go    — AES-256-GCM decrypt for credentials stored by the API
//	session.go   — per-device session cache + gosnmp v1/v2c/v3 factory
//	collector.go — Collector with CollectSystem / CollectInterfaces / …
//	scheduler.go — (future) min-heap scheduler; Phase 1 uses a fixed ticker
//
// The poller decrypts SNMPv3 auth/priv passphrases with the same
// SNMP_ENC_KEY used by the FastAPI server. Passphrases never hit disk
// inside the poller; they live in memory only for the duration of a
// session's lifetime.
package snmp

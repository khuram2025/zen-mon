package pinger

import (
	"net"
	"time"

	"github.com/google/uuid"
)

// Device represents a monitored network device.
type Device struct {
	ID           uuid.UUID
	Hostname     string
	IPAddress    net.IP
	PingInterval time.Duration
	PingEnabled  bool
	Status       string
	LastSeen     time.Time
	LastRTT      float64
	DownCount    int
}

// PingResult holds the result of a ping check for a device.
type PingResult struct {
	DeviceID   uuid.UUID
	IPAddress  net.IP
	IsUp       bool
	RTT        time.Duration
	PacketLoss float32
	Jitter     time.Duration
	MinRTT     time.Duration
	MaxRTT     time.Duration
	Sent       int
	Received   int
	Timestamp  time.Time
	PollerID   string
}

// StatusChange represents a device status transition.
type StatusChange struct {
	DeviceID  uuid.UUID
	OldStatus string
	NewStatus string
	Reason    string
	Timestamp time.Time
}

// HealthStatus represents the health of the poller engine.
type HealthStatus struct {
	Status            string `json:"status"`
	PollerID          string `json:"poller_id"`
	DeviceCount       int    `json:"device_count"`
	ServiceCheckCount int    `json:"service_check_count"`
	ActivePings       int    `json:"active_pings"`
	Uptime            string `json:"uptime"`
	LastCycleMs       int64  `json:"last_cycle_ms"`
}

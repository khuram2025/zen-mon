package checker

import (
	"time"

	"github.com/google/uuid"
)

// ServiceCheck represents a configured service check loaded from PostgreSQL.
type ServiceCheck struct {
	ID                  uuid.UUID
	DeviceID            *uuid.UUID
	Name                string
	CheckType           string // "http", "tcp", "tls"
	Enabled             bool
	TargetHost          string
	TargetPort          int
	TargetURL           string
	HTTPMethod          string
	HTTPHeaders         map[string]string
	HTTPBody            string
	HTTPExpectedStatus  int
	HTTPContentMatch    string
	HTTPFollowRedirects bool
	TLSWarnDays         int
	TLSCriticalDays     int
	CheckInterval       time.Duration
	Timeout             time.Duration
	Status              string
	DownCount           int // runtime state
}

// ServiceCheckResult holds the outcome of a single service check.
type ServiceCheckResult struct {
	ServiceCheckID   uuid.UUID
	DeviceID         *uuid.UUID
	CheckType        string
	IsUp             bool
	ResponseTime     time.Duration
	StatusCode       int    // HTTP only
	ContentMatched   *bool  // HTTP only
	TLSDaysRemaining *int   // TLS only
	TLSValid         *bool  // TLS only
	TLSExpiry        *time.Time
	TLSIssuer        string
	TLSSubject       string
	Error            string
	Timestamp        time.Time
	PollerID         string
}

// ServiceStatusChange represents a service check status transition.
type ServiceStatusChange struct {
	ServiceCheckID uuid.UUID
	DeviceID       *uuid.UUID
	CheckType      string
	OldStatus      string
	NewStatus      string
	Reason         string
	Timestamp      time.Time
}

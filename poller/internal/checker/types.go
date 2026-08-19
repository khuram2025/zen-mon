package checker

import (
	"time"

	"github.com/google/uuid"
)

// HTTPWorkflowStep is one request in a cookie-preserving service journey.
// Secret placeholders are expanded only in memory immediately before send.
type HTTPWorkflowStep struct {
	Name             string            `json:"name"`
	URL              string            `json:"url"`
	Method           string            `json:"method"`
	Headers          map[string]string `json:"headers"`
	Body             string            `json:"body"`
	ExpectedStatuses string            `json:"expected_statuses"`
	ContentMatch     string            `json:"content_match"`
	FollowRedirects  bool              `json:"follow_redirects"`
}

// ServiceCheck represents a configured service check loaded from PostgreSQL.
type ServiceCheck struct {
	ID                   uuid.UUID
	DeviceID             *uuid.UUID
	GroupID              *uuid.UUID
	ParentCheckID        *uuid.UUID
	Name                 string
	CheckType            string         // "http" | "tcp" | "tls" | "icmp" | "dns"
	Level                int            // 1 = availability, 2 = health, 3 = transaction
	Config               map[string]any // type-specific (e.g. DNS record_type, expected)
	Tags                 []string
	Enabled              bool
	TargetHost           string
	TargetPort           int
	TargetURL            string
	HTTPMethod           string
	HTTPHeaders          map[string]string
	HTTPBody             string
	HTTPExpectedStatus   int
	HTTPExpectedStatuses string // comma-separated patterns: "200,2xx,200-299". Empty = use HTTPExpectedStatus.
	HTTPContentMatch     string
	HTTPFollowRedirects  bool
	TLSWarnDays          int
	TLSCriticalDays      int
	CheckInterval        time.Duration
	Timeout              time.Duration
	RetryCount           int
	RetryDelay           time.Duration
	CredentialID         *uuid.UUID
	CredentialName       string
	CredentialAuthType   string
	CredentialUsername   string
	CredentialSecret     string
	CredentialError      string
	WorkflowOperator     string
	WorkflowSteps        []HTTPWorkflowStep
	Status               string
	DownCount            int // runtime state
	LastCheckAt          time.Time
}

// ServiceCheckResult holds the outcome of a single service check.
type ServiceCheckResult struct {
	ServiceCheckID   uuid.UUID
	DeviceID         *uuid.UUID
	CheckType        string
	IsUp             bool
	ResponseTime     time.Duration
	StatusCode       int   // HTTP only
	ContentMatched   *bool // HTTP only
	TLSDaysRemaining *int  // TLS only
	TLSValid         *bool // TLS only
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

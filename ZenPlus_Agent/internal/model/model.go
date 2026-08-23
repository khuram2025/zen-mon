package model

import "time"

const AgentVersion = "1.11.2"

var AgentCapabilities = []string{
	"network_capture_v1",
	"capture_stop_v1",
	"interface_traffic_v1",
	"apm_status_v1",
	"apm_gateway_v1",
	"apm_runtime_discovery_v1",
	"apm_iis_instrumentation_v1",
	"apm_windows_service_instrumentation_v1",
	"apm_runtime_health_v1",
}

type Metric struct {
	Kind      string         `json:"kind"`
	Timestamp time.Time      `json:"timestamp"`
	Data      map[string]any `json:"data"`
}

type EventSummary struct {
	Channel   string            `json:"channel"`
	Level     string            `json:"level"`
	Count     int               `json:"count"`
	Since     time.Time         `json:"since"`
	Labels    map[string]string `json:"labels,omitempty"`
	LastError string            `json:"last_error,omitempty"`
}

type Batch struct {
	AgentID       string         `json:"agent_id"`
	ServerID      string         `json:"server_id"`
	BatchID       string         `json:"batch_id"`
	SequenceStart uint64         `json:"sequence_start"`
	SequenceEnd   uint64         `json:"sequence_end"`
	ConfigHash    string         `json:"config_hash,omitempty"`
	AgentVersion  string         `json:"agent_version"`
	CollectedAt   time.Time      `json:"collected_at"`
	SentAt        time.Time      `json:"sent_at,omitempty"`
	Metrics       []Metric       `json:"metrics"`
	Inventory     map[string]any `json:"inventory,omitempty"`
	Events        []EventSummary `json:"events,omitempty"`
	Health        Health         `json:"-"`
}

type Health struct {
	Status               string            `json:"status"`
	LastConfigTime       *time.Time        `json:"last_config_time,omitempty"`
	LastSuccessfulUpload *time.Time        `json:"last_successful_upload,omitempty"`
	QueueDepth           int               `json:"queue_depth"`
	SpoolBytes           int64             `json:"spool_bytes"`
	CollectorErrors      map[string]string `json:"collector_errors,omitempty"`
	UploadError          string            `json:"upload_error,omitempty"`
	ConfigError          string            `json:"config_error,omitempty"`
}

type Heartbeat struct {
	Version          string             `json:"version"`
	Capabilities     []string           `json:"capabilities,omitempty"`
	UptimeSeconds    int64              `json:"uptime_seconds,omitempty"`
	QueueDepth       int                `json:"queue_depth,omitempty"`
	SpoolBytes       int64              `json:"spool_bytes,omitempty"`
	ConfigHash       string             `json:"config_hash,omitempty"`
	ConfigApplyError string             `json:"config_apply_error,omitempty"`
	APM              *AgentAPMHeartbeat `json:"apm,omitempty"`
}

type APMGatewayStatus struct {
	Listening bool   `json:"listening"`
	Healthy   bool   `json:"healthy"`
	Managed   bool   `json:"managed"`
	Version   string `json:"version,omitempty"`
	GRPCPort  int    `json:"grpc_port"`
	HTTPPort  int    `json:"http_port"`
}

// AgentAPMHeartbeat is local, endpoint-observed APM state sent to the
// appliance. It contains status only—never an ingest key or token.
type AgentAPMHeartbeat struct {
	Enabled           bool              `json:"enabled"`
	Profile           string            `json:"profile,omitempty"`
	Environment       string            `json:"environment,omitempty"`
	State             string            `json:"state,omitempty"`
	Gateway           APMGatewayStatus  `json:"gateway"`
	Discovered        int               `json:"discovered"`
	Instrumented      int               `json:"instrumented"`
	Failed            int               `json:"failed"`
	SpansForwarded1M  int               `json:"spans_forwarded_1m"`
	ExportErrors1M    int               `json:"export_errors_1m"`
	SpoolDepthSpans   int               `json:"spool_depth_spans"`
	SpoolBytes        int64             `json:"spool_bytes"`
	DroppedSpansTotal int64             `json:"dropped_spans_total"`
	Bundles           map[string]string `json:"bundles,omitempty"`
	LastError         string            `json:"last_error,omitempty"`
	CheckedAt         time.Time         `json:"checked_at"`
}

type HeartbeatResponse struct {
	OK             bool          `json:"ok"`
	ServerTime     time.Time     `json:"server_time"`
	ConfigETag     string        `json:"config_etag"`
	HasCommands    bool          `json:"has_commands"`
	DesiredVersion *string       `json:"desired_version"`
	Backpressure   *Backpressure `json:"backpressure"`
	APM            *APMStatus    `json:"apm,omitempty"`
}

// APMStatus is a read-only appliance health snapshot. The local APM monitoring
// switch is separate and never changes appliance availability.
type APMStatus struct {
	Available          bool       `json:"available"`
	State              string     `json:"state"`
	ManagedBy          string     `json:"managed_by"`
	IngestPath         string     `json:"ingest_path"`
	QueueDepth         int        `json:"queue_depth"`
	QueueCapacity      int        `json:"queue_capacity"`
	AcceptedSpansTotal int64      `json:"accepted_spans_total"`
	RejectedSpansTotal int64      `json:"rejected_spans_total"`
	DroppedSpansTotal  int64      `json:"dropped_spans_total"`
	LastReceivedAt     *time.Time `json:"last_received_at,omitempty"`
	Message            string     `json:"message,omitempty"`
	CheckedAt          time.Time  `json:"checked_at"`
}

type Backpressure struct {
	RetryAfterSeconds int    `json:"retry_after_s"`
	Reason            string `json:"reason"`
}

type EnrollmentRequest struct {
	// PendingSecret proves that repeated tokenless registration polls come
	// from the same installation. It is generated locally and protected by
	// the operating system before being stored on disk.
	PendingSecret string         `json:"pending_secret"`
	AgentUID      string         `json:"agent_uid"`
	Hostname      string         `json:"hostname"`
	Platform      string         `json:"platform"`
	Version       string         `json:"version"`
	FQDN          string         `json:"fqdn,omitempty"`
	PrimaryIP     string         `json:"primary_ip,omitempty"`
	OSName        string         `json:"os_name,omitempty"`
	OSVersion     string         `json:"os_version,omitempty"`
	KernelOrBuild string         `json:"kernel_or_build,omitempty"`
	Architecture  string         `json:"architecture"`
	InstallID     string         `json:"install_id,omitempty"`
	Inventory     map[string]any `json:"inventory,omitempty"`
}

type EnrollmentResponse struct {
	AgentID                   string `json:"agent_id"`
	ServerID                  string `json:"server_id"`
	APIKey                    string `json:"api_key"`
	AuthorizationState        string `json:"authorization_state"`
	HeartbeatIntervalSeconds  int    `json:"heartbeat_interval_s"`
	ConfigPollIntervalSeconds int    `json:"config_poll_interval_s"`
	UploadIntervalSeconds     int    `json:"upload_interval_s"`
	PolicyID                  string `json:"policy_id"`
}

type AgentConfigResponse struct {
	ConfigVersion     int                    `json:"config_version"`
	PolicyID          string                 `json:"policy_id"`
	ETag              string                 `json:"etag"`
	MetricIntervalS   int                    `json:"metric_interval_s"`
	UploadIntervalS   int                    `json:"upload_interval_s"`
	ProcessTopN       int                    `json:"process_top_n"`
	ServiceWatchlist  []string               `json:"service_watchlist"`
	ProcessWatchlist  []string               `json:"process_watchlist"`
	EventLogFilters   []EventLogFilter       `json:"event_log_filters"`
	DiskIgnore        []string               `json:"disk_ignore"`
	NetworkIgnore     []string               `json:"network_ignore"`
	CardinalityLimits map[string]int         `json:"cardinality_limits"`
	FeatureFlags      map[string]bool        `json:"feature_flags"`
	UpdateRing        string                 `json:"update_ring"`
	Signature         *string                `json:"signature"`
	SignedAt          time.Time              `json:"signed_at"`
	Raw               map[string]interface{} `json:"-"`
}

type EventLogFilter struct {
	Channel string   `json:"channel" yaml:"channel"`
	Levels  []string `json:"levels" yaml:"levels"`
	IDs     []int    `json:"ids,omitempty" yaml:"ids,omitempty"`
}

type ResultsResponse struct {
	OK           bool          `json:"ok"`
	Accepted     int           `json:"accepted"`
	Rejected     int           `json:"rejected"`
	Duplicates   int           `json:"duplicates"`
	Backpressure *Backpressure `json:"backpressure"`
	Errors       []string      `json:"errors"`
}

type CommandPoll struct {
	HasCommands bool      `json:"has_commands"`
	Commands    []Command `json:"commands"`
}

type Command struct {
	ID        string         `json:"id"`
	Command   string         `json:"command"`
	Params    map[string]any `json:"params"`
	ExpiresAt *time.Time     `json:"expires_at"`
}

type CommandResult struct {
	Success      bool           `json:"success"`
	Output       map[string]any `json:"output,omitempty"`
	ErrorMessage string         `json:"error_message,omitempty"`
}

// NetworkCaptureUpload streams flows from an in-progress or finished capture.
type NetworkCaptureUpload struct {
	CaptureID      string                    `json:"capture_id"`
	AgentID        string                    `json:"agent_id"`
	ServerID       string                    `json:"server_id"`
	Status         string                    `json:"status"` // running | completed | cancelled | failed
	Interface      string                    `json:"interface,omitempty"`
	StartedAt      time.Time                 `json:"started_at"`
	EndsAt         time.Time                 `json:"ends_at"`
	Samples        int                       `json:"samples"`
	Truncated      bool                      `json:"truncated"`
	BytesAvailable bool                      `json:"bytes_available"`
	Note           string                    `json:"note,omitempty"`
	ErrorMessage   string                    `json:"error_message,omitempty"`
	Flows          []NetworkFlow             `json:"flows"`
	Interfaces     []NetworkInterfaceTraffic `json:"interfaces,omitempty"`
}

type NetworkFlow struct {
	Protocol      string    `json:"protocol"`
	Kind          string    `json:"kind"`
	Direction     string    `json:"direction"`
	LocalIP       string    `json:"local_ip"`
	LocalPort     uint32    `json:"local_port"`
	RemoteIP      string    `json:"remote_ip"`
	RemotePort    uint32    `json:"remote_port"`
	PID           int32     `json:"pid"`
	ProcessName   string    `json:"process_name"`
	ServiceName   string    `json:"service_name,omitempty"`
	State         string    `json:"state"`
	BytesSent     uint64    `json:"bytes_sent"`
	BytesReceived uint64    `json:"bytes_received"`
	BytesKnown    bool      `json:"bytes_known"`
	FirstSeen     time.Time `json:"first_seen"`
	LastSeen      time.Time `json:"last_seen"`
	Samples       int       `json:"samples"`
}

// NetworkInterfaceTraffic is total interface traffic observed during a
// capture window. Flow rows remain local/remote socket observations; these
// counters are the authoritative all-protocol totals for interface usage.
type NetworkInterfaceTraffic struct {
	Interface            string    `json:"interface"`
	InterfaceIndex       uint32    `json:"interface_index,omitempty"`
	Timestamp            time.Time `json:"timestamp"`
	RXBytes              uint64    `json:"rx_bytes"`
	TXBytes              uint64    `json:"tx_bytes"`
	RXBPS                float64   `json:"rx_bps"`
	TXBPS                float64   `json:"tx_bps"`
	PeakRXBPS            float64   `json:"peak_rx_bps"`
	PeakTXBPS            float64   `json:"peak_tx_bps"`
	LinkSpeedBPS         uint64    `json:"link_speed_bps,omitempty"`
	ReceiveLinkSpeedBPS  uint64    `json:"receive_link_speed_bps,omitempty"`
	TransmitLinkSpeedBPS uint64    `json:"transmit_link_speed_bps,omitempty"`
	RXUtilizationPct     float64   `json:"rx_utilization_pct,omitempty"`
	TXUtilizationPct     float64   `json:"tx_utilization_pct,omitempty"`
}

type DiagnosticsRequest struct {
	AgentID      string `json:"agent_id"`
	FileName     string `json:"file_name"`
	FileSize     int64  `json:"file_size"`
	SHA256       string `json:"sha256"`
	DiagnosticID string `json:"diagnostic_id,omitempty"`
	Notes        string `json:"notes,omitempty"`
}

type Status struct {
	AgentID            string             `json:"agent_id"`
	ServerID           string             `json:"server_id"`
	ControllerURL      string             `json:"controller_url"`
	AgentVersion       string             `json:"agent_version"`
	StartedAt          time.Time          `json:"started_at"`
	LastCollection     *time.Time         `json:"last_collection,omitempty"`
	LastHeartbeat      *time.Time         `json:"last_heartbeat,omitempty"`
	LastHeartbeatError string             `json:"last_heartbeat_error,omitempty"`
	LastUpload         *time.Time         `json:"last_upload,omitempty"`
	LastUploadError    string             `json:"last_upload_error,omitempty"`
	LastConfigPoll     *time.Time         `json:"last_config_poll,omitempty"`
	LastConfigError    string             `json:"last_config_error,omitempty"`
	QueueDepth         int                `json:"queue_depth"`
	SpoolBytes         int64              `json:"spool_bytes"`
	CollectorErrors    map[string]string  `json:"collector_errors,omitempty"`
	Enrolled           bool               `json:"enrolled"`
	AuthState          string             `json:"auth_state,omitempty"` // ok | unenrolled | unauthorized
	ClockSkewSeconds   float64            `json:"clock_skew_seconds,omitempty"`
	NextRetryAt        *time.Time         `json:"next_retry_at,omitempty"`
	UpgradeState       string             `json:"upgrade_state,omitempty"`
	APM                *APMStatus         `json:"apm,omitempty"`
	LocalAPM           *AgentAPMHeartbeat `json:"local_apm,omitempty"`
}

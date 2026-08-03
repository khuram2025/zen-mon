package identity

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/shirou/gopsutil/v4/host"
)

type Identity struct {
	AgentUID      string    `json:"agent_uid"`
	AgentID       string    `json:"agent_id"`
	ServerID      string    `json:"server_id"`
	MachineGUID   string    `json:"machine_guid,omitempty"`
	Hostname      string    `json:"hostname"`
	FQDN          string    `json:"fqdn,omitempty"`
	Platform      string    `json:"platform"`
	OSName        string    `json:"os_name,omitempty"`
	OSVersion     string    `json:"os_version,omitempty"`
	KernelOrBuild string    `json:"kernel_or_build,omitempty"`
	Architecture  string    `json:"architecture"`
	PrimaryIP     string    `json:"primary_ip,omitempty"`
	BootTime      time.Time `json:"boot_time,omitempty"`
	CreatedAt     time.Time `json:"created_at"`
}

// LoadOrCreate returns the persisted identity, creating one on first run.
// The second return value reports that the underlying machine identity
// changed since the identity file was written (a golden-image clone or a
// restored VM): the agent_uid is regenerated and any previous enrollment
// (agent_id/server_id) is discarded so the clone does not fight the
// original host over one identity.
func LoadOrCreate(path string, agentID string, serverID string) (Identity, bool, error) {
	var id Identity
	if b, err := os.ReadFile(path); err == nil {
		if err := json.Unmarshal(b, &id); err == nil && (id.AgentUID != "" || id.Hostname != "") {
			detected, _ := Detect()
			if cloneDetected(id, detected) {
				fresh := detected
				if fresh.AgentUID == "" {
					fresh.AgentUID = stableAgentUID(fresh.Hostname)
				}
				if agentID != "" {
					fresh.AgentID = agentID
				} else {
					fresh.AgentID = "agt_local_" + randomHex(10)
				}
				if serverID != "" {
					fresh.ServerID = serverID
				} else {
					fresh.ServerID = "srv_local_" + stableHostHash(fresh.Hostname)
				}
				fresh.CreatedAt = time.Now().UTC()
				return fresh, true, Save(path, fresh)
			}
			id.fillMissing(detected)
			if agentID != "" {
				id.AgentID = agentID
			}
			if serverID != "" {
				id.ServerID = serverID
			}
			return id, false, Save(path, id)
		}
	}
	id, err := Detect()
	if err != nil {
		return Identity{}, false, err
	}
	if id.AgentUID == "" {
		id.AgentUID = stableAgentUID(id.Hostname)
	}
	if agentID != "" {
		id.AgentID = agentID
	} else {
		id.AgentID = "agt_local_" + randomHex(10)
	}
	if serverID != "" {
		id.ServerID = serverID
	} else {
		id.ServerID = "srv_local_" + stableHostHash(id.Hostname)
	}
	id.CreatedAt = time.Now().UTC()
	return id, false, Save(path, id)
}

// cloneDetected reports that the stored identity was minted on a different
// machine than the one we are running on now.
func cloneDetected(stored Identity, detected Identity) bool {
	if stored.MachineGUID == "" || detected.MachineGUID == "" {
		return false
	}
	return !strings.EqualFold(stored.MachineGUID, detected.MachineGUID)
}

func Detect() (Identity, error) {
	hostname, _ := os.Hostname()
	info, _ := host.Info()
	fqdn := lookupFQDN(hostname)
	id := Identity{
		AgentUID:     stableAgentUID(hostname),
		MachineGUID:  machineGUID(),
		Hostname:     hostname,
		FQDN:         fqdn,
		Platform:     normalizePlatform(runtime.GOOS),
		Architecture: runtime.GOARCH,
		PrimaryIP:    primaryIP(),
		CreatedAt:    time.Now().UTC(),
	}
	if info != nil {
		id.OSName = info.Platform
		id.OSVersion = info.PlatformVersion
		id.KernelOrBuild = info.KernelVersion
		if info.BootTime > 0 {
			id.BootTime = time.Unix(int64(info.BootTime), 0).UTC()
		}
	}
	return id, nil
}

func Save(path string, id Identity) error {
	b, err := json.MarshalIndent(id, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, b, 0o600)
}

func randomHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b)
}

func stableHostHash(hostname string) string {
	sum := sha256.Sum256([]byte(strings.ToLower(hostname)))
	return hex.EncodeToString(sum[:])[:16]
}

func stableAgentUID(hostname string) string {
	platform := normalizePlatform(runtime.GOOS)
	prefix := platform
	if platform == "windows" {
		prefix = "win"
	}
	if guid := machineGUID(); guid != "" {
		return prefix + "-" + strings.ToLower(guid)
	}
	return platform + "-" + stableHostHash(hostname)
}

func normalizePlatform(goos string) string {
	switch strings.ToLower(goos) {
	case "windows":
		return "windows"
	case "linux":
		return "linux"
	case "darwin":
		return "macos"
	default:
		return "other"
	}
}

func isContractPlatform(platform string) bool {
	switch platform {
	case "windows", "linux", "macos", "other":
		return true
	default:
		return false
	}
}

func (id *Identity) fillMissing(detected Identity) {
	if id.AgentUID == "" {
		id.AgentUID = detected.AgentUID
	}
	if id.MachineGUID == "" {
		id.MachineGUID = detected.MachineGUID
	}
	if id.Hostname == "" {
		id.Hostname = detected.Hostname
	}
	if id.FQDN == "" {
		id.FQDN = detected.FQDN
	}
	if !isContractPlatform(id.Platform) {
		id.Platform = detected.Platform
	}
	if id.OSName == "" {
		id.OSName = detected.OSName
	}
	if id.OSVersion == "" {
		id.OSVersion = detected.OSVersion
	}
	if id.KernelOrBuild == "" {
		id.KernelOrBuild = detected.KernelOrBuild
	}
	if id.Architecture == "" {
		id.Architecture = detected.Architecture
	}
	if id.PrimaryIP == "" {
		id.PrimaryIP = detected.PrimaryIP
	}
	if id.CreatedAt.IsZero() {
		id.CreatedAt = time.Now().UTC()
	}
}

func primaryIP() string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return ""
	}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, addr := range addrs {
			var ip net.IP
			switch v := addr.(type) {
			case *net.IPNet:
				ip = v.IP
			case *net.IPAddr:
				ip = v.IP
			}
			if ip == nil || ip.IsLoopback() {
				continue
			}
			if v4 := ip.To4(); v4 != nil {
				return v4.String()
			}
		}
	}
	return ""
}

func lookupFQDN(hostname string) string {
	if hostname == "" {
		return ""
	}
	addrs, err := net.LookupCNAME(hostname)
	if err == nil {
		return strings.TrimSuffix(addrs, ".")
	}
	return hostname
}

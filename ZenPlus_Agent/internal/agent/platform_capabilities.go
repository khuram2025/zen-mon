package agent

import goruntime "runtime"

// systemdServicesCapability tells the controller this agent reads systemd
// services (Linux). The controller then turns service collection on for it
// unless the policy's feature flags say otherwise; older Linux agents, which
// could only report an error, never receive that default.
const systemdServicesCapability = "systemd_services_v1"

func platformCapabilities(goos string) []string {
	if goos == "linux" {
		return []string{systemdServicesCapability}
	}
	return nil
}

var hostOS = goruntime.GOOS

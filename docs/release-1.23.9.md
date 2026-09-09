# ZenPlus 1.23.9

Device pages now explain ping degradation with the latest measured latency and packet loss, the configured limits, and the amount exceeded. The panel reads observations from the monitor that owns the device's status. Stale observations, recently changed thresholds, disabled ping monitoring and controller-managed devices are identified explicitly.

The most recent degraded event includes a nearby retained sample and recovery time when available. Current limits are not presented as historical limits if settings have changed since that event. New controller and remote-sensor status events include the measured breach and threshold directly in their reason text.

Monitoring settings now explain the strict OR rule: a responding device is degraded if latency is above its limit OR packet loss is above its limit. Equality does not trigger degradation. This uses individual ping checks, not chart averages. CPU, memory and hardware alerts use their own conditions. No threshold values are changed by the update.

Validation: controller threshold and reason tests with the Go race detector; API tests for measurement arithmetic, historical settings changes, device visibility, sensor contracts and maintenance; production dashboard bundle; no new TypeScript diagnostics compared with the existing baseline. The explanation was also checked against retained live device observations.

This appliance-only release includes the updated sensor artifact and preserves workstation/server agent packages.

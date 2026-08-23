//go:build !windows

package netiface

import (
	"context"

	gnet "github.com/shirou/gopsutil/v4/net"
)

func snapshot(ctx context.Context) ([]Counter, error) {
	stats, err := gnet.IOCountersWithContext(ctx, true)
	if err != nil {
		return nil, err
	}
	interfaces, _ := gnet.InterfacesWithContext(ctx)
	up := make(map[string]bool, len(interfaces))
	for _, iface := range interfaces {
		for _, flag := range iface.Flags {
			if flag == "up" {
				up[iface.Name] = true
			}
		}
	}
	out := make([]Counter, 0, len(stats))
	for _, stat := range stats {
		out = append(out, Counter{
			Name:             stat.Name,
			BytesReceived:    stat.BytesRecv,
			BytesSent:        stat.BytesSent,
			PacketsReceived:  stat.PacketsRecv,
			PacketsSent:      stat.PacketsSent,
			ReceiveErrors:    stat.Errin,
			TransmitErrors:   stat.Errout,
			ReceiveDiscards:  stat.Dropin,
			TransmitDiscards: stat.Dropout,
			IsUp:             up[stat.Name],
		})
	}
	return out, nil
}

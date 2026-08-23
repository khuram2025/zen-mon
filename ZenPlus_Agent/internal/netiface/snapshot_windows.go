//go:build windows

package netiface

import (
	"context"
	"unsafe"

	"golang.org/x/sys/windows"
)

func snapshot(ctx context.Context) ([]Counter, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	var table *windows.MibIfTable2
	if err := windows.GetIfTable2Ex(windows.MibIfTableNormal, &table); err != nil {
		return nil, err
	}
	if table == nil {
		return nil, nil
	}
	defer windows.FreeMibTable(unsafe.Pointer(table))
	if table.NumEntries == 0 {
		return nil, nil
	}
	rows := unsafe.Slice(&table.Table[0], table.NumEntries)
	out := make([]Counter, 0, len(rows))
	for i := range rows {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		row := &rows[i]
		out = append(out, Counter{
			Name:                 windows.UTF16ToString(row.Alias[:]),
			Description:          windows.UTF16ToString(row.Description[:]),
			InterfaceIndex:       row.InterfaceIndex,
			InterfaceID:          row.InterfaceGuid.String(),
			BytesReceived:        row.InOctets,
			BytesSent:            row.OutOctets,
			PacketsReceived:      row.InUcastPkts + row.InNUcastPkts,
			PacketsSent:          row.OutUcastPkts + row.OutNUcastPkts,
			ReceiveErrors:        row.InErrors,
			TransmitErrors:       row.OutErrors,
			ReceiveDiscards:      row.InDiscards,
			TransmitDiscards:     row.OutDiscards,
			ReceiveLinkSpeedBPS:  row.ReceiveLinkSpeed,
			TransmitLinkSpeedBPS: row.TransmitLinkSpeed,
			IsUp:                 row.OperStatus == 1, // IfOperStatusUp
		})
	}
	return out, nil
}

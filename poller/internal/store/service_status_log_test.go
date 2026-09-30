package store

import (
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/zenplus/poller/internal/checker"
)

func TestServiceStatusLogRowBindsValuesVerbatim(t *testing.T) {
	checkID := uuid.New()
	deviceID := uuid.New()
	reason := "Service check failed: it's \"broken\"\\n'); DROP TABLE service_status_log; --\nline2"
	now := time.Date(2026, 9, 30, 12, 0, 0, 0, time.FixedZone("x", 3600))

	row := serviceStatusLogRow(&checker.ServiceStatusChange{
		ServiceCheckID: checkID,
		DeviceID:       &deviceID,
		CheckType:      "http",
		OldStatus:      "up",
		NewStatus:      "down",
		Reason:         reason,
	}, 42, now)

	if len(row) != 8 {
		t.Fatalf("expected 8 column values, got %d", len(row))
	}
	if row[0] != checkID || row[1] != deviceID || row[3] != "http" || row[4] != "up" || row[5] != "down" || row[7] != uint64(42) {
		t.Fatalf("unexpected row values: %#v", row)
	}
	if row[6] != reason {
		t.Fatalf("reason was altered: %q", row[6])
	}
	if ts, ok := row[2].(time.Time); !ok || !ts.Equal(now) || ts.Location() != time.UTC {
		t.Fatalf("expected UTC timestamp equal to now, got %#v", row[2])
	}
	if strings.Contains(serviceStatusLogInsert, "VALUES") || strings.ContainsAny(serviceStatusLogInsert, "'%?") {
		t.Fatalf("insert statement must not embed values: %s", serviceStatusLogInsert)
	}
}

func TestServiceStatusLogRowUsesNilDeviceWhenUnset(t *testing.T) {
	row := serviceStatusLogRow(&checker.ServiceStatusChange{ServiceCheckID: uuid.New()}, 0, time.Now())
	if row[1] != uuid.Nil {
		t.Fatalf("expected nil device UUID, got %#v", row[1])
	}
}

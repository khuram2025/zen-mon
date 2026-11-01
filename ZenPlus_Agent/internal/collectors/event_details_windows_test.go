//go:build windows

package collectors

import (
	"context"
	"encoding/json"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

func TestNativeDetailedBookmarkResumesWithoutReplaying(t *testing.T) {
	q := detailQuery{Channel: "System", XPath: "*[System[(Level=0 or Level=1 or Level=2 or Level=3 or Level=4 or Level=5)]]"}
	c, _ := windows.UTF16PtrFromString(q.Channel)
	x, _ := windows.UTF16PtrFromString(q.XPath)
	results, _, err := procEvtQuery.Call(0, uintptr(unsafe.Pointer(c)), uintptr(unsafe.Pointer(x)), evtQueryChannelPath|evtQueryReverseDirection)
	if results == 0 {
		t.Fatal(err)
	}
	defer procEvtClose.Call(results)
	var event uintptr
	var count uint32
	ok, _, err := procEvtNext.Call(results, 1, uintptr(unsafe.Pointer(&event)), 1000, 0, uintptr(unsafe.Pointer(&count)))
	if ok == 0 {
		t.Fatal(err)
	}
	defer procEvtClose.Call(event)
	raw, err := renderEventText(event, 1)
	if err != nil {
		t.Fatal(err)
	}
	data, err := decodeDetailedEvent(q.Channel, raw, "", 2048)
	if err != nil {
		t.Fatal(err)
	}
	bookmark, _, err := procEvtCreateBookmark.Call(0)
	if bookmark == 0 {
		t.Fatal(err)
	}
	defer procEvtClose.Call(bookmark)
	ok, _, err = procEvtUpdateBookmark.Call(bookmark, event)
	if ok == 0 {
		t.Fatal(err)
	}
	xml, err := renderEventText(bookmark, 2)
	if err != nil {
		t.Fatal(err)
	}
	cursor, _ := json.Marshal(eventCursor{Bookmark: xml, UID: data["event_uid"].(string)})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	read, err := readNativeDetailedEvents(ctx, q, string(cursor), 5, 2048)
	if err != nil {
		t.Fatal(err)
	}
	if read.Gap {
		t.Fatal("valid native bookmark reported as gap")
	}
	for _, e := range read.Events {
		if e.Data["event_uid"] == data["event_uid"] {
			t.Fatal("bookmarked event replayed")
		}
	}
	// The same record ID with another event identity simulates a cleared log.
	cursor, _ = json.Marshal(eventCursor{Bookmark: xml, UID: "changed-after-log-clear"})
	read, err = readNativeDetailedEvents(ctx, q, string(cursor), 5, 2048)
	if err != nil {
		t.Fatal(err)
	}
	if !read.Gap {
		t.Fatal("record ID reuse did not report coverage gap")
	}
}

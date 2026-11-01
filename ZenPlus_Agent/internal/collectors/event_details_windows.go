//go:build windows

package collectors

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	procEvtCreateBookmark        = modwevtapi.NewProc("EvtCreateBookmark")
	procEvtUpdateBookmark        = modwevtapi.NewProc("EvtUpdateBookmark")
	procEvtSeek                  = modwevtapi.NewProc("EvtSeek")
	procEvtOpenPublisherMetadata = modwevtapi.NewProc("EvtOpenPublisherMetadata")
	procEvtFormatMessage         = modwevtapi.NewProc("EvtFormatMessage")
)

func renderEventText(handle uintptr, flags uintptr) (string, error) {
	var used, count uint32
	procEvtRender.Call(0, handle, flags, 0, 0, uintptr(unsafe.Pointer(&used)), uintptr(unsafe.Pointer(&count)))
	if used == 0 || used > 1024*1024 {
		return "", fmt.Errorf("event rendering exceeds 1 MiB safety limit")
	}
	buf := make([]uint16, (used+1)/2)
	ok, _, err := procEvtRender.Call(0, handle, flags, uintptr(len(buf)*2), uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&used)), uintptr(unsafe.Pointer(&count)))
	if ok == 0 {
		return "", err
	}
	return windows.UTF16ToString(buf), nil
}

func formatDetailedMessage(event uintptr, provider string) string {
	p, err := windows.UTF16PtrFromString(provider)
	if err != nil {
		return ""
	}
	metadata, _, _ := procEvtOpenPublisherMetadata.Call(0, uintptr(unsafe.Pointer(p)), 0, 0, 0)
	if metadata == 0 {
		return ""
	}
	defer procEvtClose.Call(metadata)
	var used uint32
	procEvtFormatMessage.Call(metadata, event, 0, 0, 0, 1, 0, 0, uintptr(unsafe.Pointer(&used)))
	if used == 0 || used > 32768 {
		return ""
	}
	buf := make([]uint16, used)
	ok, _, _ := procEvtFormatMessage.Call(metadata, event, 0, 0, 0, 1, uintptr(len(buf)), uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&used)))
	if ok == 0 {
		return ""
	}
	return windows.UTF16ToString(buf)
}

func openDetailedQuery(q detailQuery, initial bool) (uintptr, error) {
	query := q.XPath
	if initial {
		query = strings.TrimSuffix(query, "]]") + " and TimeCreated[timediff(@SystemTime) <= 300000]]]"
	}
	c, err := windows.UTF16PtrFromString(q.Channel)
	if err != nil {
		return 0, err
	}
	x, err := windows.UTF16PtrFromString(query)
	if err != nil {
		return 0, err
	}
	h, _, callErr := procEvtQuery.Call(0, uintptr(unsafe.Pointer(c)), uintptr(unsafe.Pointer(x)), evtQueryChannelPath)
	if h == 0 {
		return 0, callErr
	}
	return h, nil
}

func readNativeDetailedEvents(ctx context.Context, q detailQuery, cursor string, limit, messageBytes int) (detailRead, error) {
	out := detailRead{}
	var saved eventCursor
	if cursor != "" {
		if err := json.Unmarshal([]byte(cursor), &saved); err != nil {
			return out, fmt.Errorf("invalid saved event cursor: %w", err)
		}
	}
	results, err := openDetailedQuery(q, cursor == "")
	if err != nil {
		return out, err
	}
	defer func() { procEvtClose.Call(results) }()
	var p *uint16
	if cursor != "" {
		p, err = windows.UTF16PtrFromString(saved.Bookmark)
		if err != nil {
			return out, err
		}
	}
	bookmark, _, err := procEvtCreateBookmark.Call(uintptr(unsafe.Pointer(p)))
	if bookmark == 0 {
		return out, err
	}
	defer procEvtClose.Call(bookmark)
	skipBookmark := false
	if cursor != "" {
		ok, _, seekErr := procEvtSeek.Call(results, 0, bookmark, 0, 4|0x10000)
		if ok == 0 {
			// A rolled/cleared log is a visible coverage gap, never "clean".
			if !errors.Is(seekErr, windows.Errno(15011)) && !errors.Is(seekErr, windows.ERROR_NOT_FOUND) && !errors.Is(seekErr, errorNoMoreItems) {
				return out, seekErr
			}
			out.Gap = true
			procEvtClose.Call(results)
			results, err = openDetailedQuery(q, true)
			if err != nil {
				return out, err
			}
		} else {
			skipBookmark = true
		}
	}
	for len(out.Events) <= limit {
		if err := ctx.Err(); err != nil {
			return out, err
		}
		var event uintptr
		var returned uint32
		ok, _, nextErr := procEvtNext.Call(results, 1, uintptr(unsafe.Pointer(&event)), 100, 0, uintptr(unsafe.Pointer(&returned)))
		if ok == 0 {
			if errors.Is(nextErr, errorNoMoreItems) || errors.Is(nextErr, windows.ERROR_TIMEOUT) {
				return out, nil
			}
			return out, nextErr
		}
		if len(out.Events) == limit {
			out.More = true
			procEvtClose.Call(event)
			break
		}
		raw, renderErr := renderEventText(event, 1)
		if renderErr != nil {
			procEvtClose.Call(event)
			return out, renderErr
		}
		data, decodeErr := decodeDetailedEvent(q.Channel, raw, "", messageBytes)
		if decodeErr != nil {
			procEvtClose.Call(event)
			return out, decodeErr
		}
		if skipBookmark {
			skipBookmark = false
			procEvtClose.Call(event)
			if data["event_uid"] == saved.UID {
				continue
			}
			// Record IDs can be reused after a clear. Validate event identity too.
			out.Gap = true
			procEvtClose.Call(results)
			results, err = openDetailedQuery(q, true)
			if err != nil {
				return out, err
			}
			continue
		}
		message := formatDetailedMessage(event, data["provider"].(string))
		if message != "" {
			data["message"] = truncateUTF8(message, messageBytes)
			data["message_truncated"] = len(message) > messageBytes
		}
		updated, _, updateErr := procEvtUpdateBookmark.Call(bookmark, event)
		procEvtClose.Call(event)
		if updated == 0 {
			return out, updateErr
		}
		nextCursor, renderErr := renderEventText(bookmark, 2)
		if renderErr != nil {
			return out, renderErr
		}
		encoded, _ := json.Marshal(eventCursor{Bookmark: nextCursor, UID: data["event_uid"].(string)})
		out.Events = append(out.Events, detailEvent{Data: data, Bookmark: string(encoded)})
	}
	return out, nil
}

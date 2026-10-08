package store

import (
	"strings"
	"unicode"
)

// maxSysDescrRunes caps devices.sys_descr. Real sysDescr values are a few
// hundred characters (RFC 3418 allows 255 octets, vendors exceed it with
// build banners); the cap only guards the row against a runaway agent.
const maxSysDescrRunes = 2048

// sanitizeSysDescr makes a collected sysDescr safe and bounded for the
// devices.sys_descr text column:
//
//   - NUL bytes are removed (PostgreSQL text rejects them, and the failed
//     UPDATE would also discard the device's vendor/model/OS writeback);
//   - invalid UTF-8 is replaced with U+FFFD;
//   - surrounding whitespace is trimmed (interior line breaks are kept,
//     multi-line banners are useful evidence);
//   - the result is cut to maxSysDescrRunes runes, on a rune boundary.
//
// The SNMP collector already strips most of this, so this is the store's
// own guarantee for any caller. The result may be empty, which
// UpsertSystemInfo treats as "no value" and never writes over a stored one.
func sanitizeSysDescr(s string) string {
	// NULs first, so a NUL-padded value collapses to its text instead of
	// leaving replacement characters between the padding.
	s = strings.ReplaceAll(s, "\x00", "")
	s = strings.ToValidUTF8(s, "\uFFFD")
	s = strings.TrimSpace(s)

	// A rune takes at least one byte, so a string within the byte limit
	// cannot exceed the rune limit.
	if len(s) <= maxSysDescrRunes {
		return s
	}
	runes := 0
	for i := range s {
		if runes == maxSysDescrRunes {
			// The cut can expose whitespace that was interior before.
			return strings.TrimRightFunc(s[:i], unicode.IsSpace)
		}
		runes++
	}
	return s
}

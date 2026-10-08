package store

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestSanitizeSysDescr(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"empty", "", ""},
		{"whitespace only", " \t\r\n ", ""},
		{"plain", "Linux fw01 5.15.0-91-generic #101-Ubuntu SMP x86_64", "Linux fw01 5.15.0-91-generic #101-Ubuntu SMP x86_64"},
		{"trims surrounding whitespace", "\r\n  FortiGate-60F  \t\n", "FortiGate-60F"},
		{
			"keeps interior line breaks",
			"Cisco IOS Software, Version 15.2(4)E10\r\nTechnical Support: http://www.cisco.com/techsupport",
			"Cisco IOS Software, Version 15.2(4)E10\r\nTechnical Support: http://www.cisco.com/techsupport",
		},
		{"nul inside", "abc\x00def", "abcdef"},
		{"nul padding", "Linux host\x00\x00\x00\x00", "Linux host"},
		{"nul only", "\x00\x00\x00", ""},
		{"nul and whitespace at both ends", " \x00 abc \x00 ", "abc"},
		{"invalid byte replaced", "caf\xe9 router", "caf\uFFFD router"},
		{"truncated multi-byte sequence replaced", "ab\xe2\x82", "ab\uFFFD"},
		{"valid multi-byte untouched", "Zürich 路由器 😀", "Zürich 路由器 😀"},
		{"nul between invalid bytes", "\xff\x00\xfe", "\uFFFD"},
		{"only invalid bytes", "\xff\xfe\xfd", "\uFFFD"},
		{"exactly at the rune cap", strings.Repeat("a", maxSysDescrRunes), strings.Repeat("a", maxSysDescrRunes)},
		{"one rune over the cap", strings.Repeat("a", maxSysDescrRunes+1), strings.Repeat("a", maxSysDescrRunes)},
		{"far over the cap", strings.Repeat("ab", 5000), strings.Repeat("ab", maxSysDescrRunes/2)},
		{
			"cap counts runes not bytes (2 byte runes at the cap are kept)",
			strings.Repeat("é", maxSysDescrRunes),
			strings.Repeat("é", maxSysDescrRunes),
		},
		{
			"2 byte runes over the cap",
			strings.Repeat("é", maxSysDescrRunes+10),
			strings.Repeat("é", maxSysDescrRunes),
		},
		{
			"3 byte runes over the cap",
			strings.Repeat("路", maxSysDescrRunes+1),
			strings.Repeat("路", maxSysDescrRunes),
		},
		{
			"4 byte runes straddling the cap",
			strings.Repeat("a", maxSysDescrRunes-1) + "😀😀😀",
			strings.Repeat("a", maxSysDescrRunes-1) + "😀",
		},
		{
			"byte limit would split a rune",
			// maxSysDescrRunes bytes ends in the middle of the 4 byte rune
			// when cutting by bytes; cutting by runes keeps it whole.
			strings.Repeat("a", maxSysDescrRunes-2) + "😀😀",
			strings.Repeat("a", maxSysDescrRunes-2) + "😀😀",
		},
		{
			"whitespace exposed by the cut is trimmed",
			strings.Repeat("a", maxSysDescrRunes-1) + " bbbbb",
			strings.Repeat("a", maxSysDescrRunes-1),
		},
		{
			"a long run of invalid bytes collapses to one replacement",
			strings.Repeat("\xff", 10000),
			"\uFFFD",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := sanitizeSysDescr(c.in)
			if got != c.want {
				t.Fatalf("sanitizeSysDescr(%.60q) = %.60q (len %d), want %.60q (len %d)",
					c.in, got, len(got), c.want, len(c.want))
			}
			assertStorableSysDescr(t, got)
			if again := sanitizeSysDescr(got); again != got {
				t.Fatalf("not idempotent: second pass gave %.60q from %.60q", again, got)
			}
		})
	}
}

// assertStorableSysDescr checks what PostgreSQL and the dashboard need from
// the output, independent of the exact expected string.
func assertStorableSysDescr(t *testing.T, s string) {
	t.Helper()
	if !utf8.ValidString(s) {
		t.Fatalf("output is not valid UTF-8: %q", s)
	}
	if strings.ContainsRune(s, 0) {
		t.Fatalf("output contains a NUL byte: %q", s)
	}
	if n := utf8.RuneCountInString(s); n > maxSysDescrRunes {
		t.Fatalf("output has %d runes, want at most %d", n, maxSysDescrRunes)
	}
	if s != strings.TrimSpace(s) {
		t.Fatalf("output has surrounding whitespace: %q", s)
	}
}

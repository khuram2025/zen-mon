package checker

import "testing"

func TestTCPAddress(t *testing.T) {
	tests := []struct {
		name string
		host string
		port int
		want string
	}{
		{name: "IPv4", host: "192.0.2.10", port: 443, want: "192.0.2.10:443"},
		{name: "hostname", host: "example.test", port: 8443, want: "example.test:8443"},
		{name: "IPv6", host: "2001:db8::10", port: 443, want: "[2001:db8::10]:443"},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := tcpAddress(tt.host, tt.port); got != tt.want {
				t.Fatalf("tcpAddress(%q, %d) = %q; want %q", tt.host, tt.port, got, tt.want)
			}
		})
	}
}

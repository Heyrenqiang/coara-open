package tunnel

import "testing"

func TestExtractTunnelURL(t *testing.T) {
	tests := []struct {
		name string
		line string
		want string
	}{
		{
			name: "standard quick tunnel log",
			line: `INF |  https://abc-def-ghi.trycloudflare.com   |`,
			want: "https://abc-def-ghi.trycloudflare.com",
		},
		{
			name: "url in log line",
			line: `INF Registered tunnel connection connIndex=0 url=https://foo-bar-baz.trycloudflare.com`,
			want: "https://foo-bar-baz.trycloudflare.com",
		},
		{
			name: "no url",
			line: `INF Starting cloudflared`,
			want: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := extractTunnelURL(tt.line)
			if got != tt.want {
				t.Errorf("extractTunnelURL() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestIsAvailable(t *testing.T) {
	_ = IsAvailable("")
}

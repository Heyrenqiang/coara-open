package connect

import (
	"strings"
	"testing"

	"gomatrix/internal/config"
)

func TestEncodeQRPayload(t *testing.T) {
	raw := EncodeQRPayload("https://demo.trycloudflare.com", "coara.local", "@coara:coara.local", "phone", "secret", "registration-secret")
	if !strings.Contains(raw, `"m.server":"https://demo.trycloudflare.com"`) {
		t.Fatalf("missing m.server: %q", raw)
	}
	if !strings.Contains(raw, `"coara.server_name":"coara.local"`) {
		t.Fatalf("missing server_name: %q", raw)
	}
	if !strings.Contains(raw, `"coara.tunnel":"true"`) {
		t.Fatalf("missing tunnel flag: %q", raw)
	}
	if !strings.Contains(raw, `"m.user":"phone"`) || !strings.Contains(raw, `"m.password":"secret"`) {
		t.Fatalf("missing pairing credentials: %q", raw)
	}
	if !strings.Contains(raw, `"coara.registration_token":"registration-secret"`) {
		t.Fatalf("missing registration token: %q", raw)
	}
}

func TestDefaultBotUser(t *testing.T) {
	cfg := config.Default()
	got := DefaultBotUser(&cfg)
	if got != "@coara:coara.local" {
		t.Fatalf("DefaultBotUser() = %q", got)
	}
}

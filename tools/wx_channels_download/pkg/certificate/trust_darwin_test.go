//go:build darwin

package certificate

import "testing"

func TestIsCertificateTrustedSunnyNet(t *testing.T) {
	if !isCertificateTrusted("SunnyNet") {
		t.Fatalf("expected SunnyNet to be trusted in System keychain")
	}
}

func TestIsCertificateTrustedMitmproxy(t *testing.T) {
	// mitmproxy is typically present but untrusted on this machine; do not fail if absent.
	trusted := isCertificateTrusted("mitmproxy")
	t.Logf("mitmproxy trusted=%v", trusted)
}

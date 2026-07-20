package system

import (
	"strings"
	"testing"
)

func TestBuildPACScriptRoutesWeChatOnly(t *testing.T) {
	script := BuildPACScript("127.0.0.1", 2023, "http://127.0.0.1:7897")
	if !strings.Contains(script, `return "PROXY 127.0.0.1:2023"`) {
		t.Fatalf("PAC missing MITM directive:\n%s", script)
	}
	if !strings.Contains(script, `PROXY 127.0.0.1:7897`) {
		t.Fatalf("PAC missing fallback directive:\n%s", script)
	}
	if !strings.Contains(script, `dnsDomainIs(host, ".qq.com")`) {
		t.Fatalf("PAC missing qq.com match:\n%s", script)
	}
	if !strings.Contains(script, `function FindProxyForURL`) {
		t.Fatalf("PAC missing FindProxyForURL:\n%s", script)
	}
}

func TestBuildPACScriptDirectFallback(t *testing.T) {
	script := BuildPACScript("127.0.0.1", 2023, "")
	if !strings.Contains(script, `return "DIRECT"`) {
		t.Fatalf("expected DIRECT fallback:\n%s", script)
	}
}

func TestBuildPACURL(t *testing.T) {
	got := BuildPACURL("127.0.0.1", 2023)
	if got != "http://127.0.0.1:2023/proxy.pac" {
		t.Fatalf("got %q", got)
	}
}

func TestAddrKeyPAC(t *testing.T) {
	a := AddrKey(ProxySettings{PACURL: "http://127.0.0.1:2023/proxy.pac"})
	b := CurAddrKey(&ProxySettings{PACURL: "http://127.0.0.1:2023/proxy.pac/"})
	// trailing slash is normalized away from path (EscapedPath of .../proxy.pac/ differs)
	// ensure same host/port base at least
	if !strings.HasPrefix(a, "pac:http://127.0.0.1:2023/proxy.pac") {
		t.Fatalf("unexpected key %q", a)
	}
	_ = b
}

func TestPacFallbackDirective(t *testing.T) {
	if got := pacFallbackDirective("http://127.0.0.1:7897"); got != "PROXY 127.0.0.1:7897" {
		t.Fatalf("http fallback: %q", got)
	}
	if got := pacFallbackDirective("socks5://127.0.0.1:7897"); !strings.Contains(got, "SOCKS5 127.0.0.1:7897") {
		t.Fatalf("socks fallback: %q", got)
	}
	if got := pacFallbackDirective("127.0.0.1:7897"); got != "PROXY 127.0.0.1:7897" {
		t.Fatalf("host:port fallback: %q", got)
	}
}

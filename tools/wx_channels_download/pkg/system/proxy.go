package system

import (
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"
)

type ProxySettings struct {
	Device   string
	Hostname string
	Port     string
	// PACURL when non-empty enables Proxy Auto-Config instead of a blanket
	// HTTP/HTTPS system proxy. Only domains matched by the PAC script go
	// through the MITM proxy; everything else stays DIRECT or uses Fallback.
	// Example: http://127.0.0.1:2023/proxy.pac
	PACURL string
	// Fallback is an optional non-matched traffic proxy, e.g. "http://127.0.0.1:7897".
	// Used when generating PAC content (see BuildPACScript). Not applied by
	// EnableProxy itself.
	Fallback string
}

type HardwarePort struct {
	Device    string
	Port      string
	Interface string
}

func merge_default_settings(p ProxySettings) ProxySettings {
	if p.Device == "" {
		p.Device = "Wi-Fi" // 默认使用 Wi-Fi 设备
		device, err := get_network_interfaces()
		if err == nil {
			p.Device = device.Port
		}
	}
	if p.Hostname == "" {
		p.Hostname = "127.0.0.1"
	}
	if p.Port == "" {
		p.Port = "2023"
	}
	return p
}

func EnableProxy(arg ProxySettings) error {
	return enable_proxy(arg)
}

func DisableProxy(arg ProxySettings) error {
	return disable_proxy(arg)
}

func FetchCurProxy(arg ProxySettings) (*ProxySettings, error) {
	return fetch_cur_proxy(arg)
}

// AddrKey returns a stable identifier for the currently expected proxy mode,
// used by the watchdog that restores settings after Clash/Vortex overrides them.
func AddrKey(p ProxySettings) string {
	p = merge_default_settings(p)
	if u := strings.TrimSpace(p.PACURL); u != "" {
		return "pac:" + normalizePACURL(u)
	}
	return "manual:" + p.Hostname + ":" + p.Port
}

// CurAddrKey identifies whatever the OS currently has configured.
func CurAddrKey(cur *ProxySettings) string {
	if cur == nil {
		return ""
	}
	if u := strings.TrimSpace(cur.PACURL); u != "" {
		return "pac:" + normalizePACURL(u)
	}
	if cur.Hostname == "" || cur.Port == "" {
		return ""
	}
	return "manual:" + cur.Hostname + ":" + cur.Port
}

func normalizePACURL(raw string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	u, err := url.Parse(raw)
	if err != nil {
		return strings.TrimRight(raw, "/")
	}
	host := strings.ToLower(u.Hostname())
	port := u.Port()
	if port == "" {
		switch strings.ToLower(u.Scheme) {
		case "https":
			port = "443"
		default:
			port = "80"
		}
	}
	path := u.EscapedPath()
	if path == "" {
		path = "/"
	}
	return strings.ToLower(u.Scheme) + "://" + host + ":" + port + path
}

// BuildPACURL constructs the PAC document URL served by the local MITM proxy.
func BuildPACURL(hostname string, port int) string {
	host := strings.TrimSpace(hostname)
	if host == "" {
		host = "127.0.0.1"
	}
	if port <= 0 {
		port = 2023
	}
	return fmt.Sprintf("http://%s/proxy.pac", net.JoinHostPort(host, strconv.Itoa(port)))
}

// BuildPACScript returns a PAC FindProxyForURL script that only sends WeChat /
// Tencent-related hosts through the MITM proxy. Other traffic uses fallback
// (if set) or DIRECT, so browsers keep using the user's normal network path.
func BuildPACScript(proxyHost string, proxyPort int, fallback string) string {
	host := strings.TrimSpace(proxyHost)
	if host == "" {
		host = "127.0.0.1"
	}
	if proxyPort <= 0 {
		proxyPort = 2023
	}
	mitm := fmt.Sprintf("PROXY %s", net.JoinHostPort(host, strconv.Itoa(proxyPort)))
	rest := "DIRECT"
	if fb := pacFallbackDirective(fallback); fb != "" {
		// Fall through to DIRECT if the user proxy is down.
		rest = fb + "; DIRECT"
	}

	// Keep the host list tight: only WeChat channels / related Tencent hosts that
	// need MITM injection. Everything else (including Chrome general browsing)
	// must not go through the downloader proxy.
	return fmt.Sprintf(`// wx_channels_download selective PAC
// Only WeChat/Tencent related hosts use the MITM proxy.
// Other traffic: %s
function FindProxyForURL(url, host) {
  if (!host) return "DIRECT";
  host = host.toLowerCase();
  if (isPlainHostName(host) ||
      host === "127.0.0.1" ||
      host === "localhost" ||
      host === "::1" ||
      shExpMatch(host, "127.*") ||
      shExpMatch(host, "10.*") ||
      shExpMatch(host, "192.168.*") ||
      shExpMatch(host, "172.16.*") ||
      shExpMatch(host, "172.17.*") ||
      shExpMatch(host, "172.18.*") ||
      shExpMatch(host, "172.19.*") ||
      shExpMatch(host, "172.2*.*") ||
      shExpMatch(host, "172.30.*") ||
      shExpMatch(host, "172.31.*")) {
    return "DIRECT";
  }

  // WeChat / QQ / channels
  if (dnsDomainIs(host, ".qq.com") || host === "qq.com" ||
      dnsDomainIs(host, ".weixin.qq.com") ||
      dnsDomainIs(host, ".wechat.com") || host === "wechat.com" ||
      dnsDomainIs(host, ".weixin.com") || host === "weixin.com" ||
      dnsDomainIs(host, ".qpic.cn") || host === "qpic.cn" ||
      dnsDomainIs(host, ".qlogo.cn") || host === "qlogo.cn" ||
      dnsDomainIs(host, ".gtimg.cn") || host === "gtimg.cn" ||
      dnsDomainIs(host, ".idqqimg.com") || host === "idqqimg.com" ||
      dnsDomainIs(host, ".tencent.com") || host === "tencent.com" ||
      dnsDomainIs(host, ".tencent-cloud.com") ||
      dnsDomainIs(host, ".servicewechat.com") || host === "servicewechat.com" ||
      dnsDomainIs(host, ".tenpay.com") || host === "tenpay.com") {
    return "%s";
  }
  return "%s";
}
`, rest, mitm, rest)
}

func pacFallbackDirective(fallback string) string {
	fallback = strings.TrimSpace(fallback)
	if fallback == "" {
		return ""
	}
	// Accept raw host:port or full URLs (http:// / socks5://).
	if !strings.Contains(fallback, "://") {
		host, port, err := net.SplitHostPort(fallback)
		if err != nil {
			return ""
		}
		if host == "" || port == "" {
			return ""
		}
		return "PROXY " + net.JoinHostPort(host, port)
	}
	u, err := url.Parse(fallback)
	if err != nil || u.Hostname() == "" {
		return ""
	}
	port := u.Port()
	scheme := strings.ToLower(u.Scheme)
	if port == "" {
		switch scheme {
		case "https":
			port = "443"
		case "socks5", "socks":
			port = "1080"
		default:
			port = "80"
		}
	}
	addr := net.JoinHostPort(u.Hostname(), port)
	switch scheme {
	case "socks5", "socks":
		return "SOCKS5 " + addr + "; SOCKS " + addr
	default:
		return "PROXY " + addr
	}
}

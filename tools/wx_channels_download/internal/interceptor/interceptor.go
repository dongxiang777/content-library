package interceptor

import (
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"

	"github.com/ltaoo/echo"
	"github.com/rs/zerolog"

	"wx_channel/internal/buildtags"
	"wx_channel/internal/interceptor/proxy"
	"wx_channel/pkg/certificate"
	"wx_channel/pkg/system"
)

type Interceptor struct {
	Version           string
	Debug             bool
	Settings          *InterceptorConfig
	Headers           map[string]string
	Cert              *certificate.CertFileAndKeyFile
	proxy             proxy.InnerProxy
	PostPlugins       []interface{}  // echo 的插件，将在 echo 初始化后传给 echo
	FrontendVariables map[string]any // 前端额外的全局变量
	log               *zerolog.Logger
	OnCookies         func(url string, cookies []*http.Cookie) // 捕获到 cookie 时的回调
}

func NewInterceptor(cfg *InterceptorConfig, cert *certificate.CertFileAndKeyFile) *Interceptor {
	log := zerolog.New(io.Discard).With().Timestamp().Str("component", "interceptor").Str("version", cfg.Version).Logger()
	return &Interceptor{
		Version:           cfg.Version,
		Debug:             cfg.DebugShowError,
		Settings:          cfg,
		FrontendVariables: make(map[string]any),
		Cert:              cert,
		log:               &log,
		proxy:             nil,
	}
}

func (c *Interceptor) Start() error {
	echo.SetLogEnabled(c.Settings.EchoLogEnabled)
	client, err := proxy.NewProxy(c.Cert.Cert, c.Cert.PrivateKey, c.Settings.ProxyUpstreamProxy, c.Settings.ProxyTun, c.Settings.ProxyServerHostname, c.Settings.ProxyServerPort, c.Settings.ProxyDefaultInterface, &proxy.TCPRelayConfig{
		Enabled:  c.Settings.ProxyTCPRelayEnabled,
		Hostname: c.Settings.ProxyTCPRelayHostname,
		Port:     c.Settings.ProxyTCPRelayPort,
	})
	if err != nil {
		return err
	}
	if len(c.PostPlugins) != 0 {
		for _, plugin := range c.PostPlugins {
			client.AddPlugin(plugin)
		}
	}
	downloadTarget := &proxy.TargetConfig{
		Protocol: c.Settings.APIServerProtocol,
		Host:     c.Settings.APIServerHostname,
		Port:     c.Settings.APIServerPort,
	}
	if c.Settings.RemoteServerEnabled {
		downloadTarget = &proxy.TargetConfig{
			Protocol: c.Settings.RemoteServerProtocol,
			Host:     c.Settings.RemoteServerHostname,
			Port:     c.Settings.RemoteServerPort,
		}
	}
	client.AddPlugin(&proxy.Plugin{
		Match:  "weixin110.qq.com",
		Target: downloadTarget,
	})
	client.AddPlugin(&proxy.Plugin{
		Match: "kf.qq.com",
		Target: &proxy.TargetConfig{
			Protocol: c.Settings.APIServerProtocol,
			Host:     c.Settings.APIServerHostname,
			Port:     c.Settings.APIServerPort,
		},
	})
	// Same-origin WebSocket paths on the channels HTML host. Used when the
	// injected page connects to wss://channels.weixin.qq.com/ws/* instead of
	// the legacy kf.qq.com reverse-proxy hop (which WeChatAppEx may skip).
	channelsAPITarget := &proxy.TargetConfig{
		Protocol: c.Settings.APIServerProtocol,
		Host:     c.Settings.APIServerHostname,
		Port:     c.Settings.APIServerPort,
	}
	// Path patterns (no scheme): MITM WS requests often have empty URL.Scheme and
	// MatchPluginsForRequest defaults it to "http", so scheme-prefixed patterns miss.
	for _, wsPath := range []string{
		"channels.weixin.qq.com/ws/channels",
		"channels.weixin.qq.com/ws/downloader",
	} {
		client.AddPlugin(&proxy.Plugin{
			Match:  wsPath,
			Target: channelsAPITarget,
		})
	}
	plugins := CreateChannelInterceptorPlugins(c, Assets)
	for _, plugin := range plugins {
		client.AddPlugin(plugin)
	}
	c.proxy = client
	// Ensure the active CA is trusted as an SSL root — not merely present in the
	// keychain. A common failure mode on macOS is an imported mitmproxy CA with
	// zero TrustRoot settings, which curl may still appear to accept in some
	// setups while Chromium/WeChatAppEx rejects with "tls: unknown certificate".
	certName := c.Cert.Name
	if certName == "" {
		certName = "SunnyNet"
	}
	if !c.Settings.ProxySkipInstallRootCert {
		fmt.Printf("检查根证书信任: %s\n", certName)
		if err := certificate.EnsureCertificateTrusted(certName, c.Cert.Cert); err != nil {
			return fmt.Errorf("安装/信任证书失败: %v", err)
		}
	} else if !certificate.IsCertificateTrusted(certName) {
		fmt.Printf("警告: 根证书 '%s' 未配置 SSL TrustRoot 信任，WeChatAppEx 会拒绝 MITM 证书。\n", certName)
		fmt.Printf("      请将 config 中 proxy.skipInstallRootCert 设为 false 后重启，或手动始终信任该证书。\n")
		fmt.Printf("      也可执行: sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain <cert.pem>\n")
	}
// System proxy is enabled after the HTTP listener is up (see InterceptorServer.Start)
	// so PAC URL http://127.0.0.1:port/proxy.pac is reachable immediately.
	if err := client.Start(c.Settings.ProxyServerPort); err != nil {
		return err
	}
	return nil
}

// SystemProxySettings returns the settings used to install/restore the system proxy.
// On all platforms we prefer PAC mode so only WeChat/Tencent hosts hit the MITM
// proxy; Chrome and other apps keep normal internet access (DIRECT or upstream).
func (c *Interceptor) SystemProxySettings() system.ProxySettings {
	host := c.Settings.ProxyServerHostname
	port := c.Settings.ProxyServerPort
	return system.ProxySettings{
		Device:   c.Settings.ProxyDevice,
		Hostname: host,
		Port:     strconv.Itoa(port),
		PACURL:   system.BuildPACURL(host, port),
		Fallback: c.Settings.ProxyUpstreamProxy,
	}
}

// EnableSystemProxy installs selective PAC (preferred) or manual system proxy.
func (c *Interceptor) EnableSystemProxy() error {
	if buildtags.UsingSunnyNet || !c.Settings.ProxySetSystem || c.Settings.ProxyTun {
		return nil
	}
	if err := system.EnableProxy(c.SystemProxySettings()); err != nil {
		return fmt.Errorf("设置代理失败: %v", err)
	}
	return nil
}

// DisableSystemProxy removes the PAC / system proxy we installed.
func (c *Interceptor) DisableSystemProxy() error {
	if buildtags.UsingSunnyNet || !c.Settings.ProxySetSystem || c.Settings.ProxyTun {
		return nil
	}
	if err := system.DisableProxy(c.SystemProxySettings()); err != nil {
		return fmt.Errorf("关闭系统代理失败: %v", err)
	}
	return nil
}

func (c *Interceptor) Stop() error {
	if err := c.DisableSystemProxy(); err != nil {
		return err
	}
	if c.proxy != nil {
		if err := c.proxy.Close(); err != nil {
			return fmt.Errorf("关闭代理服务失败: %v", err)
		}
	}
	return nil
}

func (c *Interceptor) SetVersion(v string) {
	c.Version = v
}
func (c *Interceptor) AddPostPlugin(plugin interface{}) {
	c.PostPlugins = append(c.PostPlugins, plugin)
}
func (c *Interceptor) AddPlugin(plugin interface{}) {
	if c.proxy != nil {
		c.proxy.AddPlugin(plugin)
	}
}
func (c *Interceptor) AddVariable(key string, value any) {
	c.FrontendVariables[key] = value
}

func (c *Interceptor) SetLog(writer io.Writer) {
	l := zerolog.New(writer).With().Timestamp().Str("component", "interceptor").Str("version", c.Version).Logger()
	c.log = &l
}
func (c *Interceptor) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	host := r.Host
	if h, _, err := net.SplitHostPort(r.Host); err == nil {
		host = h
	}
	isLocal := false
	if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
		isLocal = true
	}
	if host == "localhost" || host == c.Settings.ProxyServerHostname {
		isLocal = true
	}
	if isLocal && r.URL.Path == "/cert" {
		w.Header().Set("Content-Type", "application/x-x509-ca-cert")
		w.Header().Set("Content-Disposition", "attachment; filename=\"SunnyNet.cer\"")
		w.Write(c.Cert.Cert)
		return
	}
	// Selective PAC: only WeChat/Tencent domains go through this MITM proxy.
	// macOS/Windows fetch this URL directly (not via the proxy).
	if isLocal && (r.URL.Path == "/proxy.pac" || r.URL.Path == "/wpad.dat") {
		script := system.BuildPACScript(
			c.Settings.ProxyServerHostname,
			c.Settings.ProxyServerPort,
			c.Settings.ProxyUpstreamProxy,
		)
		w.Header().Set("Content-Type", "application/x-ns-proxy-autoconfig")
		w.Header().Set("Cache-Control", "no-store, no-cache, must-revalidate")
		w.Write([]byte(script))
		return
	}
	if isLocal && (r.URL.Path == "/" || r.URL.Path == "") {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprintf(w, `<html><head><meta charset="utf-8"><title>wx_channels_download</title><style>@media(prefers-color-scheme:dark){body{background:#3c3c3c;color:#e0e0e0}a{color:#7cb8ff}}</style></head><body><h1>代理服务运行中</h1><p><a href="/cert">点击下载证书</a></p><p><a href="/proxy.pac">PAC 脚本</a>（仅代理微信相关域名）</p></body></html>`)
		return
	}
	c.proxy.ServeHTTP(w, r)
}

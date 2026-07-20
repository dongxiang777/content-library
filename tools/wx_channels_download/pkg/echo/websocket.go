package echo

import (
	"bufio"
	"crypto/tls"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strings"
)

// WebSocketHandler handles WebSocket upgrades
type WebSocketHandler struct {
	PluginLoader *PluginLoader
}

// HandleUpgrade handles the WebSocket upgrade request
func (h *WebSocketHandler) HandleUpgrade(w http.ResponseWriter, r *http.Request, isSecure bool) {
	hostname := r.Host
	if strings.Contains(hostname, ":") {
		hostname, _, _ = net.SplitHostPort(hostname)
	}

	path := r.URL.Path
	if r.URL.RawQuery != "" {
		path += "?" + r.URL.RawQuery
	}

	protocol := "ws"
	if isSecure {
		protocol = "wss"
	}

	log.Printf("[UPGRADE] %s %s", protocol, r.URL.String())

	// Determine target
	targetHost := r.Host
	targetProtocol := protocol
	targetPath := path

	// Find all matching plugins; apply OnRequest hooks and choose last Target
	matched_plugins := h.PluginLoader.MatchPluginsForRequest(r)
	var selected_target *TargetConfig
	if len(matched_plugins) > 0 {
		ctx := &Context{Req: r}
		for _, p := range matched_plugins {
			if p.OnRequest != nil {
				p.OnRequest(ctx)
			}
			if p.Target != nil {
				selected_target = p.Target
			}
		}
		if selected_target != nil {
			targetHost = selected_target.GetHostPort()
			targetProtocol = selected_target.Protocol
			targetPath = path

			if targetProtocol == "" {
				if selected_target.Port == 443 {
					targetProtocol = "wss"
				} else {
					targetProtocol = "ws"
				}
			}

			if targetProtocol == "http" {
				targetProtocol = "ws"
			} else if targetProtocol == "https" {
				targetProtocol = "wss"
			}
			log.Printf("[PLUGIN WS] Forwarding %s -> %s://%s%s", hostname, targetProtocol, targetHost, targetPath)
		}
	}

	// Clean up host for Dial
	dialHost := targetHost
	if !strings.Contains(dialHost, ":") {
		if targetProtocol == "wss" {
			dialHost += ":443"
		} else if targetProtocol == "ws" {
			dialHost += ":80"
		} else {
			// Fallback for other protocols
			dialHost += ":80"
		}
	}

	// Connect to backend
	var backendConn net.Conn
	var err error

	if targetProtocol == "wss" {
		// Use TLS for secure WebSocket connections
		conf := &tls.Config{InsecureSkipVerify: true}
		backendConn, err = tls.Dial("tcp", dialHost, conf)
	} else {
		// Use standard TCP for non-secure WebSocket connections (ws://)
		backendConn, err = net.Dial("tcp", dialHost)
	}

	if err != nil {
		log.Printf("[WS Error] Failed to connect to backend %s: %v", dialHost, err)
		http.Error(w, "Bad Gateway", http.StatusBadGateway)
		return
	}
	defer backendConn.Close()

	// Hijack client connection FIRST (before sending request to backend)
	hijacker, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "Hijacking not supported", http.StatusInternalServerError)
		return
	}
	clientConn, _, err := hijacker.Hijack()
	if err != nil {
		log.Printf("[WS Error] Hijack failed: %v", err)
		return
	}
	defer clientConn.Close()

	// Write the upgrade request to the backend.
	// IMPORTANT: http.Header.Write intentionally omits Host (and a few other
	// special headers). Without an explicit Host line, Go's net/http server on
	// the API backend rejects the request with:
	//   400 Bad Request: missing required Host header
	// before any gin/websocket handler runs — so MITM-proxied WSS never
	// upgrades (same-origin channels.weixin.qq.com/ws/* and kf.qq.com/ws/*).
	reqLine := fmt.Sprintf("%s %s %s\r\n", r.Method, targetPath, r.Proto)
	if _, err := backendConn.Write([]byte(reqLine)); err != nil {
		log.Printf("[WS Error] Failed to write request line: %v", err)
		return
	}

	hostHeader := targetHost
	if hostHeader == "" {
		hostHeader = r.Host
	}
	if _, err := backendConn.Write([]byte("Host: " + hostHeader + "\r\n")); err != nil {
		log.Printf("[WS Error] Failed to write Host header: %v", err)
		return
	}

	// Restore hop-by-hop headers removed by Go's http.Server when the request
	// was parsed on the MITM face. Do not put Host back into Header — Write
	// would still skip it, and we already wrote it above.
	r.Header.Del("Host")
	r.Header.Set("Connection", "Upgrade")
	r.Header.Set("Upgrade", "websocket")

	if err := r.Header.Write(backendConn); err != nil {
		log.Printf("[WS Error] Failed to write request headers: %v", err)
		return
	}
	if _, err := backendConn.Write([]byte("\r\n")); err != nil {
		log.Printf("[WS Error] Failed to finish request headers: %v", err)
		return
	}

	// Read the response status line
	bufBackend := bufio.NewReader(backendConn)
	statusLine, err := bufBackend.ReadString('\n')
	if err != nil {
		log.Printf("[WS Error] Failed to read status line: %v", err)
		return
	}

	statusLine = strings.TrimSpace(statusLine)
	log.Printf("[UPGRADE] Backend status: %s", statusLine)

	// Check if it's a 101 response
	if !strings.Contains(statusLine, "101") {
		log.Printf("[UPGRADE] Backend did not return 101, got: %s", statusLine)
		// Forward the error response to client
		clientConn.Write([]byte(statusLine + "\r\n"))
		// Copy rest of response
		io.Copy(clientConn, bufBackend)
		return
	}

	// Read and forward headers
	headers := statusLine + "\r\n"
	for {
		line, err := bufBackend.ReadString('\n')
		if err != nil {
			log.Printf("[WS Error] Failed to read header: %v", err)
			return
		}
		headers += line
		if line == "\r\n" || line == "\n" {
			break
		}
	}

	// Write 101 response to client
	clientConn.Write([]byte(headers))
	log.Printf("[UPGRADE] Sent 101 response to client, starting bidirectional copy")

	// Bidirectional copy
	go func() {
		defer backendConn.Close()
		defer clientConn.Close()
		io.Copy(clientConn, bufBackend)
	}()

	// Write to backend directly
	func() {
		defer backendConn.Close()
		defer clientConn.Close()
		io.Copy(backendConn, clientConn)
	}()
}

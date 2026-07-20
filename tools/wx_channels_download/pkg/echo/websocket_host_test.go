package echo

import (
	"bufio"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

// hijackRecorder implements http.ResponseWriter + http.Hijacker for tests.
type hijackRecorder struct {
	header http.Header
	conn   net.Conn
}

func (h *hijackRecorder) Header() http.Header {
	if h.header == nil {
		h.header = make(http.Header)
	}
	return h.header
}
func (h *hijackRecorder) Write(b []byte) (int, error) { return len(b), nil }
func (h *hijackRecorder) WriteHeader(statusCode int)  {}
func (h *hijackRecorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	return h.conn, bufio.NewReadWriter(bufio.NewReader(h.conn), bufio.NewWriter(h.conn)), nil
}

// Regression: MITM WebSocket forward must send an explicit Host line.
// http.Header.Write omits Host; without it Go's net/http returns
// "400 Bad Request: missing required Host header" before gin handlers run.
func TestWebSocketUpgradeForwardsHostHeader(t *testing.T) {
	backendLn, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer backendLn.Close()
	backendPort := backendLn.Addr().(*net.TCPAddr).Port
	backendAddr := backendLn.Addr().String()

	gotReq := make(chan string, 1)
	go func() {
		conn, err := backendLn.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		br := bufio.NewReader(conn)
		var b strings.Builder
		for {
			line, err := br.ReadString('\n')
			if err != nil {
				break
			}
			b.WriteString(line)
			if line == "\r\n" || line == "\n" {
				break
			}
		}
		gotReq <- b.String()
		_, _ = conn.Write([]byte("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"))
		// Close shortly so HandleUpgrade's bidirectional copy exits.
		time.Sleep(50 * time.Millisecond)
	}()

	// Pipe for hijacked client connection: HandleUpgrade writes 101 to clientConn.
	clientSide, serverSide := net.Pipe()
	defer clientSide.Close()
	defer serverSide.Close()
	go func() {
		buf := make([]byte, 4096)
		_, _ = clientSide.Read(buf)
		// Close to unblock HandleUpgrade copy loops.
		_ = clientSide.Close()
	}()

	loader := &PluginLoader{}
	loader.AddPlugin(&Plugin{
		Match: "channels.weixin.qq.com/ws/channels",
		Target: &TargetConfig{
			Protocol: "ws",
			Host:     "127.0.0.1",
			Port:     backendPort,
		},
	})
	h := &WebSocketHandler{PluginLoader: loader}

	req, err := http.NewRequest(http.MethodGet, "https://channels.weixin.qq.com/ws/channels", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Host = "channels.weixin.qq.com"
	req.Header.Set("Connection", "Upgrade")
	req.Header.Set("Upgrade", "websocket")
	req.Header.Set("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ==")
	req.Header.Set("Sec-WebSocket-Version", "13")

	w := &hijackRecorder{conn: serverSide}
	done := make(chan struct{})
	go func() {
		defer close(done)
		h.HandleUpgrade(w, req, true)
	}()

	select {
	case raw := <-gotReq:
		if !strings.Contains(raw, "Host: "+backendAddr) && !strings.Contains(raw, "Host: 127.0.0.1:") {
			t.Fatalf("backend request missing Host for API backend %s:\n%s", backendAddr, raw)
		}
		if !strings.Contains(strings.ToLower(raw), "upgrade: websocket") {
			t.Fatalf("backend request missing Upgrade:\n%s", raw)
		}
		// Go canonicalizes to Sec-Websocket-Key when writing headers.
		if !strings.Contains(strings.ToLower(raw), "sec-websocket-key:") {
			t.Fatalf("backend request missing Sec-WebSocket-Key:\n%s", raw)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("timeout waiting for backend request")
	}

	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("timeout waiting for HandleUpgrade to finish")
	}
}

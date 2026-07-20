package cert

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha1"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"sync"
	"time"
)

// Manager handles certificate generation and caching
type Manager struct {
	caCert       *x509.Certificate
	caKey        crypto.PrivateKey
	caCertDER    []byte
	certCache    sync.Map // map[string]*tls.Certificate
	serverKey    *rsa.PrivateKey
	serverKeyPEM []byte
}

// NewManager creates a new certificate manager
func NewManager(caCert *x509.Certificate, caKey crypto.PrivateKey) (*Manager, error) {
	// Generate a single RSA key pair for all certificates (for performance)
	serverKey, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		return nil, fmt.Errorf("failed to generate server key: %w", err)
	}

	serverKeyPEM := pem.EncodeToMemory(&pem.Block{
		Type:  "RSA PRIVATE KEY",
		Bytes: x509.MarshalPKCS1PrivateKey(serverKey),
	})

	return &Manager{
		caCert:       caCert,
		caKey:        caKey,
		caCertDER:    caCert.Raw,
		serverKey:    serverKey,
		serverKeyPEM: serverKeyPEM,
	}, nil
}

// GetCertificate returns a certificate for the given hostname, generating it if necessary
func (m *Manager) GetCertificate(hostname string) (*tls.Certificate, error) {
	hostname = normalizeHostname(hostname)
	// Check cache
	if cached, ok := m.certCache.Load(hostname); ok {
		return cached.(*tls.Certificate), nil
	}

	// Generate new certificate
	cert, err := m.generateCert(hostname)
	if err != nil {
		return nil, err
	}

	// Cache it
	m.certCache.Store(hostname, cert)
	return cert, nil
}

// GetCertificateFunc returns a function suitable for tls.Config.GetCertificate
func (m *Manager) GetCertificateFunc() func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
	return func(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
		name := hello.ServerName
		if name == "" {
			// Some clients omit SNI; fall back to a stable placeholder.
			// Callers that know the CONNECT host should prefer GetCertificate(host).
			name = "localhost"
		}
		return m.GetCertificate(name)
	}
}

func normalizeHostname(hostname string) string {
	hostname = trimSpace(hostname)
	if hostname == "" {
		return "localhost"
	}
	// Strip trailing dot from FQDN
	if len(hostname) > 1 && hostname[len(hostname)-1] == '.' {
		hostname = hostname[:len(hostname)-1]
	}
	return hostname
}

func trimSpace(s string) string {
	start, end := 0, len(s)
	for start < end && (s[start] == ' ' || s[start] == '\t' || s[start] == '\n' || s[start] == '\r') {
		start++
	}
	for end > start && (s[end-1] == ' ' || s[end-1] == '\t' || s[end-1] == '\n' || s[end-1] == '\r') {
		end--
	}
	return s[start:end]
}

// generateCert generates a new certificate for the given hostname
func (m *Manager) generateCert(hostname string) (*tls.Certificate, error) {
	// Create certificate template
	serialNumber, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return nil, fmt.Errorf("failed to generate serial number: %w", err)
	}

	// Subject Key Identifier from public key
	pubDER, err := x509.MarshalPKIXPublicKey(&m.serverKey.PublicKey)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal public key: %w", err)
	}
	ski := sha1.Sum(pubDER)

	template := &x509.Certificate{
		SerialNumber: serialNumber,
		Subject: pkix.Name{
			CommonName:   hostname,
			Organization: []string{"Echo Proxy"},
			Country:      []string{"US"},
		},
		NotBefore:             time.Now().Add(-24 * time.Hour),
		NotAfter:              time.Now().Add(365 * 24 * time.Hour),
		KeyUsage:              x509.KeyUsageKeyEncipherment | x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		IsCA:                  false,
		SubjectKeyId:          ski[:],
	}

	// Prefer parent SKI as Authority Key Id when available
	if len(m.caCert.SubjectKeyId) > 0 {
		template.AuthorityKeyId = m.caCert.SubjectKeyId
	}

	// Add SAN (Subject Alternative Name) — required by modern Chromium/WebView
	if ip := net.ParseIP(hostname); ip != nil {
		template.IPAddresses = []net.IP{ip}
		template.DNSNames = nil
	} else {
		template.DNSNames = []string{hostname}
	}

	// Sign the certificate with CA
	certDER, err := x509.CreateCertificate(rand.Reader, template, m.caCert, &m.serverKey.PublicKey, m.caKey)
	if err != nil {
		return nil, fmt.Errorf("failed to create certificate: %w", err)
	}

	// Encode leaf certificate to PEM
	certPEM := pem.EncodeToMemory(&pem.Block{
		Type:  "CERTIFICATE",
		Bytes: certDER,
	})

	// Append CA certificate so clients that do not consult the OS trust
	// store for intermediates still receive a complete chain. Roots that are
	// already trusted are ignored by compliant clients; untrusted clients may
	// still reject, but chain completeness avoids "unable to get local issuer".
	if len(m.caCertDER) > 0 {
		caPEM := pem.EncodeToMemory(&pem.Block{
			Type:  "CERTIFICATE",
			Bytes: m.caCertDER,
		})
		certPEM = append(certPEM, caPEM...)
	}

	// Create tls.Certificate (leaf + CA)
	tlsCert, err := tls.X509KeyPair(certPEM, m.serverKeyPEM)
	if err != nil {
		return nil, fmt.Errorf("failed to create TLS certificate: %w", err)
	}

	// Ensure Certificate field is populated for GetCertificate consumers
	if len(tlsCert.Certificate) == 0 {
		tlsCert.Certificate = [][]byte{certDER}
		if len(m.caCertDER) > 0 {
			tlsCert.Certificate = append(tlsCert.Certificate, m.caCertDER)
		}
	}

	return &tlsCert, nil
}

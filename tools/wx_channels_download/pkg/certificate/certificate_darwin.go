//go:build darwin

package certificate

import (
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
)

func fetchCertificates() ([]Certificate, error) {
	cmd := exec.Command("security", "find-certificate", "-a")
	output, err2 := cmd.Output()
	if err2 != nil {
		return nil, fmt.Errorf("获取证书时发生错误，%v\n", err2.Error())
	}
	var certificates []Certificate
	lines := strings.Split(string(output), "\n")
	for i := 0; i < len(lines)-1; i += 13 {
		if lines[i] == "" {
			continue
		}
		cenc := lines[i+5]
		ctyp := lines[i+6]
		hpky := lines[i+7]
		labl := lines[i+9]
		subj := lines[i+12]
		re := regexp.MustCompile(`="([^"]{1,})"`)
		matches := re.FindStringSubmatch(labl)
		if len(matches) < 1 {
			continue
		}
		label := matches[1]
		certificates = append(certificates, Certificate{
			Thumbprint: "",
			Subject: CertificateSubject{
				CN: label,
				OU: cenc,
				O:  ctyp,
				L:  hpky,
				S:  subj,
				C:  cenc,
			},
		})
	}
	return certificates, nil
}

// isCertificateTrusted reports whether a cert with the given common name/label
// is present in the admin trust domain with TrustRoot (or TrustAsRoot) result.
// Presence in the keychain alone is NOT enough — Chromium/WeChatAppEx require
// explicit SSL trust settings.
func isCertificateTrusted(certificateName string) bool {
	certificateName = strings.TrimSpace(certificateName)
	if certificateName == "" {
		return false
	}
	// Admin domain (system keychain trusts)
	if trustDumpContainsRoot(certificateName, true) {
		return true
	}
	// User domain (login keychain trusts) — some sandboxed apps read this
	if trustDumpContainsRoot(certificateName, false) {
		return true
	}
	return false
}

func trustDumpContainsRoot(certificateName string, admin bool) bool {
	args := []string{"dump-trust-settings"}
	if admin {
		args = append(args, "-d")
	}
	out, err := exec.Command("security", args...).CombinedOutput()
	if err != nil {
		// exit code 1 with "No Trust Settings" is normal
		if len(out) == 0 {
			return false
		}
	}
	text := string(out)
	// Parse blocks like:
	//   Cert N: <name>
	//      ...
	//      Result Type : kSecTrustSettingsResultTrustRoot
	blocks := strings.Split(text, "Cert ")
	for _, block := range blocks {
		// First line: "0: SunnyNet" or "1: mitmproxy"
		lineEnd := strings.IndexByte(block, '\n')
		if lineEnd < 0 {
			continue
		}
		header := block[:lineEnd]
		// header like "0: mitmproxy"
		colon := strings.IndexByte(header, ':')
		if colon < 0 {
			continue
		}
		name := strings.TrimSpace(header[colon+1:])
		if !strings.EqualFold(name, certificateName) {
			continue
		}
		if strings.Contains(block, "kSecTrustSettingsResultTrustRoot") ||
			strings.Contains(block, "kSecTrustSettingsResultTrustAsRoot") {
			// Prefer blocks that mention SSL policy, but TrustRoot without
			// policy also means "always trust" for all policies.
			return true
		}
	}
	return false
}

func writeTempCertPEM(certData []byte) (string, error) {
	pemBytes, err := normalizeCertPEM(certData)
	if err != nil {
		return "", err
	}
	tmp, err := os.CreateTemp("", "wx-channels-ca-*.pem")
	if err != nil {
		return "", fmt.Errorf("没有创建证书的权限，%v", err)
	}
	path := tmp.Name()
	if _, err := tmp.Write(pemBytes); err != nil {
		tmp.Close()
		os.Remove(path)
		return "", fmt.Errorf("写入证书失败，%v", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(path)
		return "", fmt.Errorf("关闭证书文件失败，%v", err)
	}
	return path, nil
}

// normalizeCertPEM accepts PEM or DER and returns PEM-encoded certificate bytes.
func normalizeCertPEM(certData []byte) ([]byte, error) {
	certData = bytesTrimSpace(certData)
	if len(certData) == 0 {
		return nil, fmt.Errorf("证书数据为空")
	}
	// Already PEM?
	if bytesContains(certData, []byte("-----BEGIN CERTIFICATE-----")) {
		block, _ := pem.Decode(certData)
		if block == nil {
			return nil, fmt.Errorf("无法解析 PEM 证书")
		}
		if _, err := x509.ParseCertificate(block.Bytes); err != nil {
			return nil, fmt.Errorf("证书内容无效: %v", err)
		}
		return pem.EncodeToMemory(block), nil
	}
	// Try DER
	if _, err := x509.ParseCertificate(certData); err != nil {
		return nil, fmt.Errorf("证书既不是 PEM 也不是 DER: %v", err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certData}), nil
}

func bytesTrimSpace(b []byte) []byte {
	start, end := 0, len(b)
	for start < end && (b[start] == ' ' || b[start] == '\t' || b[start] == '\n' || b[start] == '\r') {
		start++
	}
	for end > start && (b[end-1] == ' ' || b[end-1] == '\t' || b[end-1] == '\n' || b[end-1] == '\r') {
		end--
	}
	return b[start:end]
}

func bytesContains(b, sub []byte) bool {
	return strings.Contains(string(b), string(sub))
}

func runSecurity(args ...string) (string, error) {
	cmd := exec.Command("security", args...)
	out, err := cmd.CombinedOutput()
	return string(out), err
}

func installCertificate(certData []byte) error {
	path, err := writeTempCertPEM(certData)
	if err != nil {
		return err
	}
	defer os.Remove(path)

	// 1) System keychain as trust root (admin domain) — required for most apps
	//    and for Chromium/WeChatAppEx MacTrustStore.
	if out, err := runSecurity(
		"add-trusted-cert", "-d", "-r", "trustRoot",
		"-k", "/Library/Keychains/System.keychain",
		path,
	); err != nil {
		// Fallback: try via bash with quoting (older pattern)
		cmd := fmt.Sprintf(
			"security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain %q",
			path,
		)
		ps := exec.Command("bash", "-c", cmd)
		out2, err2 := ps.CombinedOutput()
		if err2 != nil {
			return fmt.Errorf("安装系统根证书失败（需要管理员权限）: %v\n%s\n%s", err2, out, string(out2))
		}
	}

	// 2) Login keychain trust (user domain) — helps some sandboxed processes
	//    that primarily read the user trust domain.
	loginKC := filepath.Join(os.Getenv("HOME"), "Library/Keychains/login.keychain-db")
	if _, err := os.Stat(loginKC); err != nil {
		loginKC = filepath.Join(os.Getenv("HOME"), "Library/Keychains/login.keychain")
	}
	if _, err := os.Stat(loginKC); err == nil {
		// Ignore errors: user-domain install is best-effort and may prompt.
		_, _ = runSecurity(
			"add-trusted-cert", "-r", "trustRoot",
			"-k", loginKC,
			path,
		)
	}

	return nil
}

// ensureCertificateTrusted installs the certificate when missing or untrusted.
// certificateName is the CN/label used for trust lookup (e.g. "SunnyNet", "mitmproxy").
func ensureCertificateTrusted(certificateName string, certData []byte) error {
	if isCertificateTrusted(certificateName) {
		return nil
	}
	// If an untrusted copy already exists, still re-apply trust settings.
	fmt.Printf("证书 '%s' 未受信任（或不存在），正在安装/修复信任设置...\n", certificateName)
	if err := installCertificate(certData); err != nil {
		return err
	}
	if !isCertificateTrusted(certificateName) {
		// Name mismatch: trust may be stored under a different label (subject CN).
		// Try to extract CN from cert data for a second check.
		if cn := certCommonName(certData); cn != "" && !strings.EqualFold(cn, certificateName) {
			if isCertificateTrusted(cn) {
				return nil
			}
		}
		return fmt.Errorf(
			"证书已写入钥匙串，但未能确认 TrustRoot 信任设置。\n"+
				"请手动双击证书，在「钥匙串访问」中将「使用此证书时」设为「始终信任」。\n"+
				"也可执行: sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain <cert.pem>",
		)
	}
	fmt.Printf("证书 '%s' 信任设置已就绪\n", certificateName)
	return nil
}

func certCommonName(certData []byte) string {
	pemBytes, err := normalizeCertPEM(certData)
	if err != nil {
		return ""
	}
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return ""
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return ""
	}
	return cert.Subject.CommonName
}

func uninstallCertificate(certificateName string) error {
	certificates, err := fetchCertificates()
	if err != nil {
		return err
	}
	var matched *Certificate
	for _, cert := range certificates {
		if cert.Subject.CN == certificateName {
			matched = &cert
			break
		}
	}
	if matched == nil {
		return fmt.Errorf("没有找到匹配的根证书")
	}
	// Delete from both system and login keychains when possible
	cmds := [][]string{
		{"delete-certificate", "-c", certificateName, "/Library/Keychains/System.keychain"},
		{"delete-certificate", "-c", certificateName},
	}
	var lastErr error
	deleted := false
	for _, args := range cmds {
		if out, err := runSecurity(args...); err != nil {
			lastErr = fmt.Errorf("删除证书时发生错误，%v\n%s", err, out)
		} else {
			deleted = true
		}
	}
	if !deleted && lastErr != nil {
		return lastErr
	}
	return nil
}

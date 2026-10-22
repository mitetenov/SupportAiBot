// runtimeprobe is a host-built diagnostic mounted into the application image.
package main

import (
	"crypto/sha256"
	"crypto/x509"
	"debug/elf"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"
)

func probe() error {
	ca, e := os.ReadFile("/etc/ssl/certs/ca-certificates.crt")
	if e != nil {
		return e
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		return fmt.Errorf("no system roots")
	}
	subjects := map[string]bool{}
	for rest := ca; len(rest) > 0; {
		block, next := pem.Decode(rest)
		if block == nil {
			break
		}
		rest = next
		if block.Type != "CERTIFICATE" {
			continue
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err == nil {
			subjects[string(cert.RawSubject)] = true
		}
	}
	binary, e := elf.Open("/app/supportbot")
	if e != nil {
		return e
	}
	defer binary.Close()
	for _, p := range binary.Progs {
		if p.Type == elf.PT_INTERP {
			return fmt.Errorf("dynamic interpreter present")
		}
	}
	libs, e := binary.ImportedLibraries()
	if e != nil {
		return e
	}
	if len(libs) > 0 {
		return fmt.Errorf("dynamic dependencies present")
	}
	addresses, e := net.LookupHost("host.docker.internal")
	if e != nil || len(addresses) == 0 {
		return fmt.Errorf("container DNS failed")
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, e := client.Get(os.Getenv("TEST_HTTPS_URL"))
	if e != nil {
		return fmt.Errorf("local HTTPS fixture unavailable")
	}
	body, e := io.ReadAll(resp.Body)
	resp.Body.Close()
	if e != nil || resp.StatusCode != 200 || string(body) != "local TLS fixture" {
		return fmt.Errorf("HTTPS fixture mismatch")
	}
	faq, e := os.ReadFile("faq/faq.json")
	if e != nil || !json.Valid(faq) {
		return fmt.Errorf("FAQ unavailable")
	}
	png, e := os.ReadFile("faq/images/happ-buttons.png")
	if e != nil || len(png) < 8 {
		return fmt.Errorf("FAQ image unavailable")
	}
	cwd, _ := os.Getwd()
	home, _ := os.UserHomeDir()
	if cwd != "/app" || home != "/app" || os.Getuid() != 100 || os.Getgid() != 101 {
		return fmt.Errorf("identity/cwd/home drift")
	}
	sum := sha256.Sum256(ca)
	return json.NewEncoder(os.Stdout).Encode(map[string]any{"static_elf": true, "machine": binary.Machine.String(), "ca_roots": len(subjects), "ca_sha256": hex.EncodeToString(sum[:]), "dns": true, "https": true, "uid": os.Getuid(), "gid": os.Getgid(), "home": home, "cwd": cwd, "faq_json": true, "faq_png": true})
}
func main() {
	if e := probe(); e != nil {
		fmt.Fprintln(os.Stderr, e)
		os.Exit(1)
	}
}

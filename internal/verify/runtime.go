package verify

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"time"
)

func Runtime(ctx context.Context, image, baseline string) (map[string]any, error) {
	files, e := archive(ctx, image)
	if e != nil {
		return nil, e
	}
	ca := files["etc/ssl/certs/ca-certificates.crt"]
	if len(ca) == 0 {
		return nil, fmt.Errorf("image has no CA bundle")
	}
	config, e := inspect(ctx, image, true)
	if e != nil {
		return nil, e
	}
	if baseline != "" {
		old, e := archive(ctx, baseline)
		if e != nil {
			return nil, e
		}
		if string(ca) != string(old["etc/ssl/certs/ca-certificates.crt"]) {
			return nil, fmt.Errorf("default CA bundle changed")
		}
	}
	dir, e := os.MkdirTemp("", "supportbot-runtime-")
	if e != nil {
		return nil, e
	}
	defer os.RemoveAll(dir)
	if e = os.Chmod(dir, 0755); e != nil {
		return nil, e
	}
	build := exec.CommandContext(ctx, goBinary(), "build", "-trimpath", "-ldflags=-s -w", "-o", filepath.Join(dir, "probe"), "./cmd/runtimeprobe")
	build.Env = append(os.Environ(), "CGO_ENABLED=0", "GOOS=linux", "GOARCH="+config.Architecture)
	if out, e := build.CombinedOutput(); e != nil {
		return nil, fmt.Errorf("build runtime probe: %s", out)
	}
	key, e := rsa.GenerateKey(rand.Reader, 2048)
	if e != nil {
		return nil, e
	}
	now := time.Now()
	template := &x509.Certificate{SerialNumber: big.NewInt(now.UnixNano()), Subject: pkix.Name{CommonName: "host.docker.internal"}, DNSNames: []string{"host.docker.internal"}, NotBefore: now.Add(-time.Minute), NotAfter: now.Add(time.Hour), KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment | x509.KeyUsageCertSign, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, BasicConstraintsValid: true, IsCA: true}
	der, e := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if e != nil {
		return nil, e
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(key)})
	cert, e := tls.X509KeyPair(certPEM, keyPEM)
	if e != nil {
		return nil, e
	}
	bundle := append(append(append([]byte{}, ca...), '\n'), certPEM...)
	if e = os.WriteFile(filepath.Join(dir, "bundle.crt"), bundle, 0644); e != nil {
		return nil, e
	}
	listener, e := net.Listen("tcp", "0.0.0.0:0")
	if e != nil {
		return nil, e
	}
	server := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { fmt.Fprint(w, "local TLS fixture") }), TLSConfig: &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = server.Serve(tls.NewListener(listener, server.TLSConfig)) }()
	defer server.Close()
	port := listener.Addr().(*net.TCPAddr).Port
	raw, e := docker(ctx, "run", "--rm", "--read-only", "--add-host=host.docker.internal:host-gateway", "--entrypoint", "/probe/probe", "-v", dir+":/probe:ro", "-e", "SSL_CERT_FILE=/probe/bundle.crt", "-e", fmt.Sprintf("TEST_HTTPS_URL=https://host.docker.internal:%d", port), image)
	if e != nil {
		return nil, e
	}
	var result map[string]any
	if e = json.Unmarshal(raw, &result); e != nil {
		return nil, e
	}
	result["image"] = image
	result["image_bytes"] = config.Size
	result["runtime_layers"] = len(config.RootFS.Layers)
	result["readonly_rootfs"] = true
	result["default_ca_bytes"] = len(ca)
	result["default_ca_matches_baseline"] = baseline != ""
	return result, nil
}

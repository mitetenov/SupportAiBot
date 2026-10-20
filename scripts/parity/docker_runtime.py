"""Verify static ELF, CA trust, DNS, HTTPS, UID and assets in a read-only image.

Uses a disposable local TLS server and an additional fixture CA; never calls an
external provider. The runtime probe is built in a temporary directory, not added
to the application or image. Requires Go, Docker and OpenSSL on the test host.
"""

import hashlib
import http.server
import io
import json
import os
import ssl
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path

IMAGE = os.environ.get("SUPPORTBOT_TEST_IMAGE", "supportbot-go:optimized")
BASELINE = "supportbot-go:parity"
GO = os.environ.get("GO_BINARY", "go")
PROBE = r"""
package main
import("crypto/sha256";"crypto/x509";"debug/elf";"encoding/hex";"encoding/json";"fmt";"io";"net";"net/http";"os";"time")
func main(){
 fail:=func(e error){fmt.Fprintln(os.Stderr,e);os.Exit(1)}
 ca,e:=os.ReadFile("/etc/ssl/certs/ca-certificates.crt");if e!=nil{fail(e)}
 roots:=x509.NewCertPool();if !roots.AppendCertsFromPEM(ca)||len(roots.Subjects())==0{fail(fmt.Errorf("no system roots"))}
 binary,e:=elf.Open("/app/supportbot");if e!=nil{fail(e)};defer binary.Close()
 for _,p:=range binary.Progs{if p.Type==elf.PT_INTERP{fail(fmt.Errorf("dynamic interpreter present"))}}
 libs,e:=binary.ImportedLibraries();if e!=nil{fail(e)};if len(libs)>0{fail(fmt.Errorf("dynamic dependencies present"))}
 addresses,e:=net.LookupHost("host.docker.internal");if e!=nil||len(addresses)==0{fail(fmt.Errorf("DNS: %v",e))}
 client:=&http.Client{Timeout:5*time.Second};response,e:=client.Get(os.Getenv("TEST_HTTPS_URL"));if e!=nil{fail(e)};body,e:=io.ReadAll(response.Body);response.Body.Close();if e!=nil||response.StatusCode!=200||string(body)!="local TLS fixture"{fail(fmt.Errorf("HTTPS fixture failed"))}
 faq,e:=os.ReadFile("faq/faq.json");if e!=nil||!json.Valid(faq){fail(fmt.Errorf("FAQ unavailable"))}
 png,e:=os.ReadFile("faq/images/happ-buttons.png");if e!=nil||len(png)<8{fail(fmt.Errorf("FAQ image unavailable"))}
 cwd,_:=os.Getwd();home,_:=os.UserHomeDir();if cwd!="/app"||home!="/app"||os.Getuid()!=100||os.Getgid()!=101{fail(fmt.Errorf("identity/cwd/home drift"))}
 sum:=sha256.Sum256(ca);json.NewEncoder(os.Stdout).Encode(map[string]any{"static_elf":true,"machine":binary.Machine.String(),"ca_roots":len(roots.Subjects()),"ca_sha256":hex.EncodeToString(sum[:]),"dns":true,"https":true,"uid":os.Getuid(),"gid":os.Getgid(),"home":home,"cwd":cwd,"faq_json":true,"faq_png":true})
}
"""


def docker(*args, binary=False):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=not binary, check=True
    ).stdout


def image_ca(image):
    container = docker("create", image).strip()
    try:
        exported = docker("export", container, binary=True)
        with tarfile.open(fileobj=io.BytesIO(exported)) as archive:
            return archive.extractfile("etc/ssl/certs/ca-certificates.crt").read()
    finally:
        docker("rm", container)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body = b"local TLS fixture"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    baseline_ca, optimized_ca = image_ca(BASELINE), image_ca(IMAGE)
    assert baseline_ca == optimized_ca, "default public trust store changed"
    config = json.loads(docker("image", "inspect", IMAGE))[0]
    with tempfile.TemporaryDirectory(prefix="supportbot-tls-probe-") as directory:
        path = Path(directory)
        # Nonroot container must be able to traverse the host-mounted probe dir.
        path.chmod(0o755)
        source = path / "main.go"
        source.write_text(PROBE)
        env = {**os.environ, "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": config["Architecture"]}
        subprocess.run(
            [GO, "build", "-trimpath", "-ldflags=-s -w", "-o", str(path / "probe"), str(source)],
            env=env,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(path / "key.pem"),
                "-out",
                str(path / "cert.pem"),
                "-days",
                "1",
                "-subj",
                "/CN=host.docker.internal",
                "-addext",
                "subjectAltName=DNS:host.docker.internal",
            ],
            check=True,
            capture_output=True,
        )
        bundle = path / "bundle.crt"
        bundle.write_bytes(optimized_ca + b"\n" + (path / "cert.pem").read_bytes())
        bundle.chmod(0o644)
        server = http.server.ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(path / "cert.pem", path / "key.pem")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = json.loads(
                docker(
                    "run",
                    "--rm",
                    "--read-only",
                    "--entrypoint",
                    "/probe/probe",
                    "-v",
                    f"{path}:/probe:ro",
                    "-e",
                    "SSL_CERT_FILE=/probe/bundle.crt",
                    "-e",
                    f"TEST_HTTPS_URL=https://host.docker.internal:{server.server_port}",
                    IMAGE,
                )
            )
            result.update(
                {
                    "image": IMAGE,
                    "default_ca_matches_baseline": True,
                    "default_ca_bytes": len(optimized_ca),
                    "image_bytes": config["Size"],
                    "runtime_layers": len(config["RootFS"]["Layers"]),
                    "readonly_rootfs": True,
                }
            )
            assert result["ca_sha256"] == hashlib.sha256(optimized_ca).hexdigest()
            print(json.dumps(result, indent=2))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()

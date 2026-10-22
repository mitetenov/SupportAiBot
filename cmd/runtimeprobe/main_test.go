package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"testing"
	"time"
)

func TestCertificateCountDistinctCertificatesWithSameSubject(t *testing.T) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	var bundle []byte
	for _, serial := range []int64{1, 2, 1} {
		cert := &x509.Certificate{SerialNumber: big.NewInt(serial), Subject: pkix.Name{CommonName: "same subject"}, NotBefore: time.Unix(0, 0), NotAfter: time.Unix(10000, 0)}
		// Repeat exactly the first DER certificate, rather than creating a new signature.
		if serial == 1 && len(bundle) > 0 {
			first, _ := pem.Decode(bundle)
			bundle = append(bundle, pem.EncodeToMemory(first)...)
			continue
		}
		raw, err := x509.CreateCertificate(rand.Reader, cert, cert, &key.PublicKey, key)
		if err != nil {
			t.Fatal(err)
		}
		bundle = append(bundle, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: raw})...)
	}
	first, _ := pem.Decode(bundle)
	bundle = append(bundle, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Headers: map[string]string{"Ignored": "header"}, Bytes: first.Bytes})...)
	bundle = append(bundle, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: []byte("invalid")})...)
	if got := certificateCount(bundle); got != 2 {
		t.Fatalf("count = %d, want 2 distinct certificates despite shared subject", got)
	}
}

package logging

import (
	"strings"
	"testing"
)

func TestRedactionAcrossErrorsAndURLs(t *testing.T) {
	r := NewRedactor("very-secret-custom-token")
	s := r.Text(`request https://api.telegram.org/bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdef/getFile?token=sensitive https://u:pass@db.example/ Authorization: Bearer very-secret-custom-token`)
	for _, secret := range []string{"very-secret-custom-token", "sensitive", "ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdef", ":pass@"} {
		if strings.Contains(s, secret) {
			t.Fatal("secret escaped redaction", s)
		}
	}
}

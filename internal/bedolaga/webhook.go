package bedolaga

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"supportbot/internal/diagnostics"
)

func SignatureMatches(secret string, body []byte, header string) bool {
	if secret == "" {
		return true
	}
	if header == "" {
		return false
	}
	m := hmac.New(sha256.New, []byte(secret))
	m.Write(body)
	supplied := strings.TrimSpace(strings.TrimPrefix(header, "sha256="))
	return hmac.Equal([]byte(hex.EncodeToString(m.Sum(nil))), []byte(supplied))
}

type Scheduler interface{ Schedule(int64) }

func Webhook(answerer Scheduler, secret string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx := diagnostics.WithOperation(diagnostics.WithCorrelation(r.Context(), diagnostics.NewID()), "bedolaga", "webhook")
		r = r.WithContext(ctx)
		respond := func(status int, text string) {
			diagnostics.Payload(ctx, "Bedolaga webhook outcome", map[string]any{"status": status, "outcome": text})
			if status >= 400 {
				diagnostics.Error(ctx, "Bedolaga webhook rejected", nil, "http_status", status)
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(status)
			_ = json.NewEncoder(w).Encode(map[string]string{"status": text})
		}
		if r.Method != "POST" {
			respond(405, "method not allowed")
			return
		}
		body, e := io.ReadAll(io.LimitReader(r.Body, 1<<20+1))
		if e != nil || len(body) > 1<<20 {
			respond(400, "bad request")
			return
		}
		diagnostics.Payload(ctx, "Bedolaga webhook payload", body)
		if !SignatureMatches(secret, body, r.Header.Get("X-Webhook-Signature")) {
			respond(403, "forbidden")
			return
		}
		event := r.Header.Get("X-Webhook-Event")
		if event != "ticket.created" && event != "ticket.message_added" {
			respond(200, "ignored")
			return
		}
		if len(body) == 0 {
			body = []byte("{}")
		}
		var p map[string]json.RawMessage
		if json.Unmarshal(body, &p) != nil || p == nil {
			respond(400, "bad request")
			return
		}
		var admin bool
		_ = json.Unmarshal(p["is_from_admin"], &admin)
		if admin {
			respond(200, "ignored")
			return
		}
		id, ok := parseID(p["ticket_id"])
		if !ok {
			respond(400, "bad request")
			return
		}
		answerer.Schedule(id)
		respond(200, "accepted")
	}
}

package main

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// These run without Redis or a cluster - the degraded paths the app promises
// (page still loads, /status zeros out, /count 503s) are exactly what's
// testable here.

func TestRootServesPage(t *testing.T) {
	h := newMux()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/", nil))
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "k3s-demo - live autoscaling") {
		t.Errorf("code %d, body does not contain the page title", rec.Code)
	}
}

func TestUnknownPath404s(t *testing.T) {
	h := newMux()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/nope", nil))
	if rec.Code != 404 {
		t.Errorf("code %d, want 404", rec.Code)
	}
}

func TestInfoShape(t *testing.T) {
	h := newMux()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/info", nil))
	var got map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"service", "version", "greeting", "pod", "uptime_seconds", "secret_loaded", "redis"} {
		if _, ok := got[key]; !ok {
			t.Errorf("missing key %q", key)
		}
	}
	if got["service"] != "k3s-demo" {
		t.Errorf("service = %v", got["service"])
	}
}

func TestStatusDegradesWithoutRedis(t *testing.T) {
	h := newMux()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/status", nil))
	if rec.Code != 200 {
		t.Fatalf("code %d, want 200 even with Redis down", rec.Code)
	}
	var got map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got["active_pods"] != float64(0) || got["count"] != float64(0) {
		t.Errorf("expected zeroed counters, got %v", got)
	}
	if got["cpu_pct"] != nil {
		t.Errorf("cpu_pct should be null outside a cluster, got %v", got["cpu_pct"])
	}
	if got["hpa_target"] != float64(70) || got["max_replicas"] != float64(6) {
		t.Errorf("constants wrong: %v", got)
	}
}

func TestCount503sWithoutRedis(t *testing.T) {
	h := newMux()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/count", nil))
	if rec.Code != 503 || rec.Body.String() != "redis unavailable" {
		t.Errorf("code %d body %q", rec.Code, rec.Body.String())
	}
}

func TestBurnCapsAndReturnsPod(t *testing.T) {
	h := newMux()
	rec := httptest.NewRecorder()
	began := time.Now()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/burn?ms=50", nil))
	if elapsed := time.Since(began); elapsed < 50*time.Millisecond || elapsed > time.Second {
		t.Errorf("burn took %v, want ~50ms", elapsed)
	}
	if !strings.HasPrefix(rec.Body.String(), hostname+" ") {
		t.Errorf("body %q does not start with hostname", rec.Body.String())
	}
}

func TestHealthz(t *testing.T) {
	h := newMux()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/healthz", nil))
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"status":"ok"`) {
		t.Errorf("code %d body %q", rec.Code, rec.Body.String())
	}
}

func TestReadyzGatesOnUptime(t *testing.T) {
	h := newMux()
	origStart := start
	defer func() { start = origStart }()

	start = time.Now() // just booted
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/readyz", nil))
	if rec.Code != 503 {
		t.Errorf("fresh boot: code %d, want 503", rec.Code)
	}

	start = time.Now().Add(-3 * time.Second) // past the 2s gate
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/readyz", nil))
	if rec.Code != 200 {
		t.Errorf("after gate: code %d, want 200", rec.Code)
	}
}

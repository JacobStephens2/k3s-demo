// k3s-demo: a small two-tier app that demonstrates Kubernetes operations. The
// root page lets a visitor generate CPU load and watch - on a live chart - the
// CPU climb past the HorizontalPodAutoscaler's 70% target and the app scale
// 2 -> 6 pods in response.
//
// Active pods come from Redis (a sorted set of recently-active pods); the CPU%
// and replica counts come from reading the HPA object via the in-cluster
// Kubernetes API (a ServiceAccount with read-only RBAC on pods + HPAs).
//
// A Go rewrite of the original Python/FastAPI service: same endpoints, same
// JSON shapes, same HTML page - but it compiles to a single static binary
// that ships in a FROM scratch image (no shell, no interpreter, no libc).
// GOMAXPROCS is cgroup-aware out of the box on Go 1.25+, so the runtime
// respects the container's 250m CPU limit without tuning.
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

//go:embed index.html
var page []byte

const (
	activeKey    = "k3s-demo:active_pods"
	hitsKey      = "k3s-demo:hits"
	activeWindow = 15 // seconds a pod stays "active" after serving a request
	saDir        = "/var/run/secrets/kubernetes.io/serviceaccount"
	hpaTarget    = 70
	maxReplicas  = 6
)

var (
	start       = time.Now()
	hostname, _ = os.Hostname()
	version     = envOr("APP_VERSION", "dev")
	greeting    = envOr("GREETING", "hello from k3s")
	hasToken    = os.Getenv("API_TOKEN") != ""
	redisAddr   = envOr("REDIS_HOST", "redis") + ":" + envOr("REDIS_PORT", "6379")
	rdb         = redis.NewClient(&redis.Options{Addr: redisAddr, DialTimeout: 2 * time.Second})
)

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// readHPA reads CPU% and replica counts from the HPA via the in-cluster API.
// Returns nils if RBAC/API is unavailable, so the page degrades gracefully.
func readHPA() (cpuPct, current, desired *int) {
	token, err := os.ReadFile(saDir + "/token")
	if err != nil {
		return nil, nil, nil
	}
	ns, err := os.ReadFile(saDir + "/namespace")
	if err != nil {
		return nil, nil, nil
	}
	caCert, err := os.ReadFile(saDir + "/ca.crt")
	if err != nil {
		return nil, nil, nil
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caCert) {
		return nil, nil, nil
	}
	host := os.Getenv("KUBERNETES_SERVICE_HOST")
	port := envOr("KUBERNETES_SERVICE_PORT_HTTPS", "443")
	url := fmt.Sprintf("https://%s:%s/apis/autoscaling/v2/namespaces/%s/horizontalpodautoscalers/k3s-demo",
		host, port, string(ns))

	client := &http.Client{
		Timeout:   2 * time.Second,
		Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}},
	}
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, nil, nil
	}
	req.Header.Set("Authorization", "Bearer "+string(token))
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, nil
	}
	defer resp.Body.Close()

	var body struct {
		Status struct {
			CurrentReplicas *int `json:"currentReplicas"`
			DesiredReplicas *int `json:"desiredReplicas"`
			CurrentMetrics  []struct {
				Resource *struct {
					Name    string `json:"name"`
					Current struct {
						AverageUtilization *int `json:"averageUtilization"`
					} `json:"current"`
				} `json:"resource"`
			} `json:"currentMetrics"`
		} `json:"status"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, nil, nil
	}
	for _, m := range body.Status.CurrentMetrics {
		if m.Resource != nil && m.Resource.Name == "cpu" {
			cpuPct = m.Resource.Current.AverageUtilization
		}
	}
	return cpuPct, body.Status.CurrentReplicas, body.Status.DesiredReplicas
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v) //nolint:errcheck // client gone; nothing to do
}

func newMux() http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Write(page) //nolint:errcheck
	})

	mux.HandleFunc("/info", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{
			"service": "k3s-demo", "version": version, "greeting": greeting,
			"pod":            hostname,
			"uptime_seconds": math.Round(time.Since(start).Seconds()*10) / 10,
			"secret_loaded":  hasToken, "redis": redisAddr,
		})
	})

	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		ctx := r.Context()
		pods, count := int64(0), 0
		now := float64(time.Now().UnixNano()) / 1e9
		if err := rdb.ZRemRangeByScore(ctx, activeKey, "0", fmt.Sprint(now-activeWindow)).Err(); err == nil {
			pods, _ = rdb.ZCard(ctx, activeKey).Result()
			if v, err := rdb.Get(ctx, hitsKey).Result(); err == nil {
				count, _ = strconv.Atoi(v)
			}
		}
		cpuPct, current, desired := readHPA()
		writeJSON(w, map[string]any{
			"active_pods": pods, "count": count, "served_by": hostname,
			"cpu_pct": cpuPct, "hpa_target": hpaTarget,
			"current_replicas": current, "desired_replicas": desired,
			"max_replicas": maxReplicas,
		})
	})

	mux.HandleFunc("/count", func(w http.ResponseWriter, r *http.Request) {
		n, err := rdb.Incr(r.Context(), hitsKey).Result()
		if err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			w.Write([]byte("redis unavailable")) //nolint:errcheck
			return
		}
		writeJSON(w, map[string]any{"count": n, "served_by": hostname})
	})

	mux.HandleFunc("/burn", func(w http.ResponseWriter, r *http.Request) {
		ms := 300
		if v, err := strconv.Atoi(r.URL.Query().Get("ms")); err == nil {
			ms = v
		}
		if ms > 2000 {
			ms = 2000
		}
		end := time.Now().Add(time.Duration(ms) * time.Millisecond)
		n := 0
		for time.Now().Before(end) {
			n++
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprintf(w, "%s %d", hostname, n)
	})

	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]string{"status": "ok"})
	})

	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		if time.Since(start) > 2*time.Second {
			w.WriteHeader(http.StatusOK)
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	})

	// Every request marks this pod active in Redis, which is what the page's
	// "active pods" tiles count. Fire-and-forget: it's best-effort bookkeeping,
	// so a slow or absent Redis must never add latency to the request path
	// (the Python original did this write synchronously and stalled when
	// Redis was unreachable).
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			score := float64(time.Now().UnixNano()) / 1e9
			rdb.ZAdd(ctx, activeKey, redis.Z{Score: score, Member: hostname}) //nolint:errcheck
		}()
		mux.ServeHTTP(w, r)
	})
}

func main() {
	srv := &http.Server{
		Addr:              ":8000",
		Handler:           newMux(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	fmt.Printf("k3s-demo %s listening on :8000 (pod %s)\n", version, hostname)
	if err := srv.ListenAndServe(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

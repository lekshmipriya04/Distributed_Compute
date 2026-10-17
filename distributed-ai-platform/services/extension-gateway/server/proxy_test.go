package server

import (
	"bytes"
	"io"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type seen struct {
	method, path, query, auth, contentType string
	body                                   []byte
}

func fakeBackend(t *testing.T, handle func(w http.ResponseWriter, r *http.Request)) (*httptest.Server, *[]seen) {
	t.Helper()
	var calls []seen
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		calls = append(calls, seen{r.Method, r.URL.Path, r.URL.RawQuery, r.Header.Get("Authorization"),
			r.Header.Get("Content-Type"), b})
		handle(w, r)
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

func proxyServer(t *testing.T, sshURL, storageURL string) *Server {
	t.Helper()
	p, err := NewProxy(ProxyConfig{SSHURL: sshURL, StorageURL: storageURL})
	if err != nil {
		t.Fatal(err)
	}
	return New(Config{Store: NewMemStore(), Proxy: p})
}

func TestProxyForwardsTrainingAndEngines(t *testing.T) {
	backend, calls := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"run-1","engine":"spark_ray"}`))
	})
	s := proxyServer(t, backend.URL, "")

	w := do(t, s, "POST", "/training", "tok-1", map[string]any{"engine": "spark_ray", "model_type": "spark:KMeans"})
	if w.Code != http.StatusCreated || !strings.Contains(w.Body.String(), `"spark_ray"`) {
		t.Fatalf("POST /training = %d %s", w.Code, w.Body.String())
	}
	got := (*calls)[0]
	if got.method != "POST" || got.path != "/api/v1/ssh/train" || got.auth != "Bearer tok-1" {
		t.Fatalf("forwarded %+v", got)
	}
	if !bytes.Contains(got.body, []byte(`"engine":"spark_ray"`)) || got.contentType != "application/json" {
		t.Fatalf("body/content-type not forwarded: %s %q", got.body, got.contentType)
	}

	do(t, s, "GET", "/engines?refresh=true", "tok-1", nil)
	do(t, s, "GET", "/engines/spark-ray/models", "tok-1", nil)
	if (*calls)[1].path != "/api/v1/ssh/engines" || (*calls)[1].query != "refresh=true" {
		t.Fatalf("engines forwarded as %+v", (*calls)[1])
	}
	if (*calls)[2].path != "/api/v1/ssh/engines/spark_ray/models" {
		t.Fatalf("models forwarded as %s", (*calls)[2].path)
	}
}

func TestProxyResolvesEachRunID(t *testing.T) {
	backend, calls := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{}`))
	})
	s := proxyServer(t, backend.URL, "")
	do(t, s, "GET", "/training/aaa", "t", nil)
	do(t, s, "GET", "/training/bbb", "t", nil)
	do(t, s, "DELETE", "/training/ccc", "t", nil)
	want := []string{"/api/v1/ssh/train/aaa", "/api/v1/ssh/train/bbb", "/api/v1/ssh/train/ccc"}
	for i, path := range want {
		if (*calls)[i].path != path {
			t.Fatalf("call %d went to %s, want %s", i, (*calls)[i].path, path)
		}
	}
}

func TestProxyStreamsModelDownload(t *testing.T) {
	zip := []byte("PK\x03\x04binary-model")
	backend, _ := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/zip")
		w.Header().Set("Content-Disposition", `attachment; filename="spark-model-1.zip"`)
		_, _ = w.Write(zip)
	})
	s := proxyServer(t, backend.URL, "")
	w := do(t, s, "GET", "/training/1/model", "t", nil)
	if w.Code != 200 || !bytes.Equal(w.Body.Bytes(), zip) {
		t.Fatalf("download = %d %q", w.Code, w.Body.Bytes())
	}
	if w.Header().Get("Content-Disposition") == "" || w.Header().Get("Content-Type") != "application/zip" {
		t.Fatalf("headers not passed: %v", w.Header())
	}
}

func TestProxyMapsBackendErrors(t *testing.T) {
	backend, _ := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method == http.MethodPost {
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"detail":"Spark + Ray is not installed on the master."}`))
			return
		}
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"detail":"Traceback secret"}`))
	})
	s := proxyServer(t, backend.URL, "")

	w := do(t, s, "POST", "/training", "t", map[string]any{})
	e := decodeBody[apiError](t, w)
	if w.Code != 400 || e.Error != "Spark + Ray is not installed on the master." || e.Code != "invalid_request" {
		t.Fatalf("400 rendered as %d %+v", w.Code, e)
	}
	w = do(t, s, "GET", "/training", "t", nil)
	if w.Code != http.StatusBadGateway || strings.Contains(w.Body.String(), "secret") {
		t.Fatalf("5xx must not leak: %d %s", w.Code, w.Body.String())
	}
}

func TestProxyUploadsDatasetToStorage(t *testing.T) {
	storage, calls := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"ds-1"}`))
	})
	ssh, _ := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {})
	s := proxyServer(t, ssh.URL, storage.URL)

	var buf bytes.Buffer
	mw := multipart.NewWriter(&buf)
	_ = mw.WriteField("name", "housing")
	fw, _ := mw.CreateFormFile("file", "housing.csv")
	_, _ = fw.Write([]byte("x,y\n1,3\n"))
	_ = mw.Close()
	r := httptest.NewRequest("POST", "/datasets", bytes.NewReader(buf.Bytes()))
	r.Header.Set("Authorization", "Bearer up-tok")
	r.Header.Set("Content-Type", mw.FormDataContentType())
	w := httptest.NewRecorder()
	s.Handler().ServeHTTP(w, r)

	if w.Code != http.StatusCreated {
		t.Fatalf("upload = %d %s", w.Code, w.Body.String())
	}
	got := (*calls)[0]
	if got.path != "/api/v1/storage/datasets" || got.auth != "Bearer up-tok" {
		t.Fatalf("upload forwarded as %+v", got)
	}
	if !strings.HasPrefix(got.contentType, "multipart/form-data; boundary=") || !bytes.Contains(got.body, []byte("1,3")) {
		t.Fatalf("multipart body not streamed: %q", got.contentType)
	}

	do(t, s, "GET", "/datasets?limit=5", "up-tok", nil)
	if (*calls)[1].path != "/api/v1/storage/datasets" || (*calls)[1].query != "limit=5" {
		t.Fatalf("list forwarded as %+v", (*calls)[1])
	}
}

func TestProxyRoutesWithoutUpstreams(t *testing.T) {
	s := newTestServer()
	if w := do(t, s, "GET", "/training", "t", nil); w.Code != http.StatusNotImplemented {
		t.Fatalf("no proxy: /training = %d", w.Code)
	}
	ssh, _ := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {})
	s = proxyServer(t, ssh.URL, "")
	w := do(t, s, "GET", "/datasets", "t", nil)
	if w.Code != http.StatusNotImplemented || !strings.Contains(w.Body.String(), "STORAGE_URL") {
		t.Fatalf("no storage: /datasets = %d %s", w.Code, w.Body.String())
	}
	if w := do(t, s, "GET", "/training", "", nil); w.Code != http.StatusUnauthorized {
		t.Fatalf("proxied routes still need a token, got %d", w.Code)
	}
}

func TestNewProxyRejectsBadURLs(t *testing.T) {
	if _, err := NewProxy(ProxyConfig{SSHURL: "ssh-executor:8011"}); err == nil {
		t.Fatal("expected an error for a URL without a scheme")
	}
	if _, err := NewProxy(ProxyConfig{SSHURL: "http://a:1", StorageURL: "ftp://b"}); err == nil {
		t.Fatal("expected an error for a non-http storage URL")
	}
}

func TestProxyForwardsNodeLifecycleAndExecutionTasks(t *testing.T) {
	backend, calls := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	s := proxyServer(t, backend.URL, "")
	cases := []struct{ method, path, upstream string }{
		{"POST", "/nodes/connect", "/api/v1/ssh/nodes/connect"},
		{"POST", "/nodes/provision", "/api/v1/ssh/nodes/provision"},
		{"POST", "/nodes/provision/manual", "/api/v1/ssh/nodes/provision/manual"},
		{"POST", "/nodes/provision/p-1/verify", "/api/v1/ssh/nodes/provision/p-1/verify"},
		{"POST", "/nodes", "/api/v1/ssh/nodes"},
		{"DELETE", "/nodes/n-1", "/api/v1/ssh/nodes/n-1"},
		{"POST", "/nodes/n-1/health-check", "/api/v1/ssh/nodes/n-1/health-check"},
		{"POST", "/nodes/n-1/refresh", "/api/v1/ssh/nodes/n-1/refresh"},
		{"POST", "/nodes/n-1/rotate-key", "/api/v1/ssh/nodes/n-1/rotate-key"},
		{"GET", "/executions/e-1/tasks", "/api/v1/ssh/batches/e-1/tasks"},
		{"POST", "/executions/e-1/retry", "/api/v1/ssh/batches/e-1/retry"},
	}
	for i, c := range cases {
		var body any
		if c.method == "POST" {
			body = map[string]any{}
		}
		w := do(t, s, c.method, c.path, "tok-1", body)
		if w.Code != http.StatusOK {
			t.Fatalf("%s %s = %d %s", c.method, c.path, w.Code, w.Body.String())
		}
		got := (*calls)[i]
		if got.method != c.method || got.path != c.upstream || got.auth != "Bearer tok-1" {
			t.Fatalf("%s %s forwarded as %+v", c.method, c.path, got)
		}
	}
}

func TestProxyKeepsNodeErrorDetail(t *testing.T) {
	status := http.StatusUnauthorized
	backend, _ := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"detail":"The worker refused the password"}`))
	})
	s := proxyServer(t, backend.URL, "")

	w := do(t, s, "POST", "/nodes/provision", "tok-1", map[string]any{"password": "x"})
	if w.Code != http.StatusBadRequest || !strings.Contains(w.Body.String(), "ssh_auth_failed") ||
		!strings.Contains(w.Body.String(), "refused the password") {
		t.Fatalf("401 from provision = %d %s", w.Code, w.Body.String())
	}

	status = http.StatusBadGateway
	w = do(t, s, "POST", "/nodes/connect", "tok-1", map[string]any{"host": "h"})
	if w.Code != http.StatusBadGateway || !strings.Contains(w.Body.String(), "node_unreachable") {
		t.Fatalf("502 from connect = %d %s", w.Code, w.Body.String())
	}

	// Non-node routes keep the generic mapping.
	status = http.StatusUnauthorized
	w = do(t, s, "GET", "/training", "tok-1", nil)
	if w.Code != http.StatusUnauthorized {
		t.Fatalf("401 from training = %d", w.Code)
	}
}

func TestNodeStatusUpdateIsValidated(t *testing.T) {
	store := NewMemStore()
	store.SeedNode(ComputeNode{ID: "n-1", Name: "w", Status: "online"})
	s := New(Config{Store: store})
	w := do(t, s, "PATCH", "/nodes/n-1", "tok-1", map[string]any{"status": "draining"})
	if w.Code != http.StatusOK || !strings.Contains(w.Body.String(), `"status":"draining"`) {
		t.Fatalf("drain = %d %s", w.Code, w.Body.String())
	}
	w = do(t, s, "PATCH", "/nodes/n-1", "tok-1", map[string]any{"status": "offline"})
	if w.Code != http.StatusBadRequest {
		t.Fatalf("invalid status = %d", w.Code)
	}
}

func TestProxyKeepsStorageErrorsReadable(t *testing.T) {
	status := http.StatusServiceUnavailable
	body := `{"error_code":"STORAGE_UNAVAILABLE","message":"This dataset is stored in HDFS, which is not running."}`
	backend, _ := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	s := proxyServer(t, "http://ssh.invalid", backend.URL)
	w := do(t, s, "GET", "/datasets", "tok-1", nil)
	if w.Code != http.StatusServiceUnavailable || !strings.Contains(w.Body.String(), "HDFS, which is not running") {
		t.Fatalf("503 = %d %s", w.Code, w.Body.String())
	}
	status = http.StatusInsufficientStorage
	body = `{"message":"Not enough free disk space to store the dataset"}`
	w = do(t, s, "GET", "/datasets", "tok-1", nil)
	if w.Code != http.StatusInsufficientStorage || !strings.Contains(w.Body.String(), "free disk space") {
		t.Fatalf("507 = %d %s", w.Code, w.Body.String())
	}
}

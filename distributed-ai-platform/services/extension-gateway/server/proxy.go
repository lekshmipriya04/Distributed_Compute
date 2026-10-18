package server

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Proxy forwards node onboarding and lifecycle, execution logs and retry,
// and the training, engine and dataset routes to ssh-executor and
// storage-service unchanged (their JSON shapes, files and errors), adding the
// caller's bearer token and request id. Bodies stream in both directions, so
// dataset uploads and model downloads are not buffered in the gateway.
type Proxy struct {
	ssh      string // e.g. http://ssh-executor:8011/api/v1/ssh
	storage  string // e.g. http://storage-service:8001/api/v1/storage ("" = not configured)
	client   *http.Client
	timeout  time.Duration
	transfer time.Duration
	logger   *slog.Logger
}

// ProxyConfig configures a Proxy.
type ProxyConfig struct {
	SSHURL     string // ssh-executor origin
	StorageURL string // storage-service origin; optional
	// Timeout bounds JSON calls (default 120s: the first Spark call starts Spark).
	Timeout time.Duration
	// TransferTimeout bounds uploads and downloads (default 30m).
	TransferTimeout time.Duration
	Client          *http.Client
	Logger          *slog.Logger
}

const (
	maxJSONBodyBytes   = 4 << 20  // training requests carry model code (1 MB max upstream)
	maxUploadBodyBytes = 10 << 30 // storage-service's default upload limit
	maxErrorBodyBytes  = 64 << 10
)

// NewProxy validates the origins and builds a Proxy.
func NewProxy(cfg ProxyConfig) (*Proxy, error) {
	sshBase, err := originWithPrefix(cfg.SSHURL, "/api/v1/ssh")
	if err != nil {
		return nil, err
	}
	storageBase := ""
	if strings.TrimSpace(cfg.StorageURL) != "" {
		if storageBase, err = originWithPrefix(cfg.StorageURL, "/api/v1/storage"); err != nil {
			return nil, err
		}
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 120 * time.Second
	}
	if cfg.TransferTimeout <= 0 {
		cfg.TransferTimeout = 30 * time.Minute
	}
	if cfg.Client == nil {
		cfg.Client = &http.Client{}
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	return &Proxy{
		ssh: sshBase, storage: storageBase, client: cfg.Client,
		timeout: cfg.Timeout, transfer: cfg.TransferTimeout, logger: cfg.Logger,
	}, nil
}

func originWithPrefix(raw, prefix string) (string, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return "", fmt.Errorf("upstream URL %q must be an absolute http(s) URL", raw)
	}
	base := strings.TrimRight(u.String(), "/")
	if !strings.HasSuffix(base, prefix) {
		base += prefix
	}
	return base, nil
}

type proxyRoute struct {
	pattern  string
	target   func(p *Proxy, r *http.Request) (string, bool)
	transfer bool // long-running upload/download
	upload   bool
	// nodeErrors keeps SSH failures readable: a refused password is not a
	// bad platform token, and an unreachable worker should say why.
	nodeErrors bool
}

func sshPath(path string) func(*Proxy, *http.Request) (string, bool) {
	return func(p *Proxy, r *http.Request) (string, bool) {
		resolved := path
		for _, name := range []string{"id", "taskId"} {
			if v := r.PathValue(name); v != "" {
				resolved = strings.Replace(resolved, "{"+name+"}", url.PathEscape(v), 1)
			}
		}
		return p.ssh + resolved, true
	}
}

func storagePath(path string) func(*Proxy, *http.Request) (string, bool) {
	return func(p *Proxy, _ *http.Request) (string, bool) {
		if p.storage == "" {
			return "", false
		}
		return p.storage + path, true
	}
}

var proxyRoutes = []proxyRoute{
	// Node onboarding (host key, one-time password or manual key, register)
	// and lifecycle. Detection over SSH can take a while; the JSON timeout
	// (120s) covers it.
	{pattern: "POST /nodes/connect", target: sshPath("/nodes/connect"), nodeErrors: true},
	{pattern: "POST /nodes/provision", target: sshPath("/nodes/provision"), nodeErrors: true},
	{pattern: "POST /nodes/provision/manual", target: sshPath("/nodes/provision/manual"), nodeErrors: true},
	{pattern: "POST /nodes/provision/{id}/verify", target: sshPath("/nodes/provision/{id}/verify"), nodeErrors: true},
	{pattern: "POST /nodes", target: sshPath("/nodes"), nodeErrors: true},
	{pattern: "DELETE /nodes/{id}", target: sshPath("/nodes/{id}"), nodeErrors: true},
	{pattern: "POST /nodes/{id}/health-check", target: sshPath("/nodes/{id}/health-check"), nodeErrors: true},
	{pattern: "POST /nodes/{id}/refresh", target: sshPath("/nodes/{id}/refresh"), nodeErrors: true},
	{pattern: "POST /nodes/{id}/rotate-key", target: sshPath("/nodes/{id}/rotate-key"), nodeErrors: true},
	// Per-task output of an execution, and running it again.
	{pattern: "GET /executions/{id}/tasks", target: sshPath("/batches/{id}/tasks")},
	{pattern: "POST /executions/{id}/retry", target: sshPath("/batches/{id}/retry")},
	// Projects: upload the folder once (.tar.gz, id = sha256), check whether
	// it is already there, and download what a task wrote to its outputs.
	{pattern: "POST /bundles", target: sshPath("/bundles"), transfer: true, upload: true},
	{pattern: "GET /bundles/{id}", target: sshPath("/bundles/{id}")},
	{pattern: "GET /executions/{id}/tasks/{taskId}/outputs", target: sshPath("/batches/{id}/tasks/{taskId}/outputs"), transfer: true},
	{pattern: "GET /engines", target: sshPath("/engines")},
	{pattern: "GET /engines/spark-ray/models", target: sshPath("/engines/spark_ray/models")},
	{pattern: "GET /training", target: sshPath("/train")},
	{pattern: "POST /training", target: sshPath("/train")},
	{pattern: "GET /training/{id}", target: sshPath("/train/{id}")},
	{pattern: "DELETE /training/{id}", target: sshPath("/train/{id}")},
	{pattern: "GET /training/{id}/model", target: sshPath("/train/{id}/model"), transfer: true},
	{pattern: "GET /datasets", target: storagePath("/datasets")},
	{pattern: "POST /datasets", target: storagePath("/datasets"), transfer: true, upload: true},
}

func (s *Server) proxyRoutes() {
	for _, route := range proxyRoutes {
		route := route
		s.mux.HandleFunc(route.pattern, func(w http.ResponseWriter, r *http.Request) {
			if s.cfg.Proxy == nil {
				s.writeError(w, r, http.StatusNotImplemented, "not_implemented",
					"Training and datasets need the gateway to be connected to ssh-executor (UPSTREAM_URL)")
				return
			}
			s.cfg.Proxy.serve(s, route, w, r)
		})
	}
}

// Forwarded request headers. Hop-by-hop headers and cookies never pass.
var forwardRequestHeaders = []string{"Content-Type", "Content-Length", "Accept"}

// Returned response headers.
var forwardResponseHeaders = []string{"Content-Type", "Content-Length", "Content-Disposition", "Cache-Control"}

func (p *Proxy) serve(s *Server, route proxyRoute, w http.ResponseWriter, r *http.Request) {
	target, ok := route.target(p, r)
	if !ok {
		s.writeError(w, r, http.StatusNotImplemented, "not_implemented",
			"Datasets need the gateway to be connected to storage-service (STORAGE_URL)")
		return
	}
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}

	timeout := p.timeout
	limit := int64(maxJSONBodyBytes)
	if route.transfer {
		timeout = p.transfer
		// Lift the server-wide read/write deadlines for this request only.
		rc := http.NewResponseController(w)
		deadline := time.Now().Add(p.transfer)
		_ = rc.SetReadDeadline(deadline)
		_ = rc.SetWriteDeadline(deadline)
	}
	if route.upload {
		limit = maxUploadBodyBytes
	}
	ctx, cancel := context.WithTimeout(r.Context(), timeout)
	defer cancel()

	var body io.Reader
	if r.Body != nil && r.Method != http.MethodGet && r.Method != http.MethodDelete {
		body = http.MaxBytesReader(w, r.Body, limit)
	}
	req, err := http.NewRequestWithContext(ctx, r.Method, target, body)
	if err != nil {
		s.writeError(w, r, http.StatusInternalServerError, "internal", "Internal error")
		return
	}
	for _, h := range forwardRequestHeaders {
		if v := r.Header.Get(h); v != "" {
			req.Header.Set(h, v)
		}
	}
	if r.ContentLength > 0 {
		req.ContentLength = r.ContentLength
	}
	if body != nil && !route.upload && req.Header.Get("Content-Type") == "" {
		// Every non-upload route takes JSON.
		req.Header.Set("Content-Type", "application/json")
	}
	if tok := TokenFromContext(r.Context()); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	if id, _ := r.Context().Value(requestIDKey).(string); id != "" {
		req.Header.Set("X-Request-Id", id)
	}

	resp, err := p.client.Do(req)
	if err != nil {
		var tooLarge *http.MaxBytesError
		switch {
		case errors.As(err, &tooLarge):
			s.writeError(w, r, http.StatusRequestEntityTooLarge, "too_large", "The request is too large")
		case errors.Is(ctx.Err(), context.DeadlineExceeded):
			s.writeError(w, r, http.StatusGatewayTimeout, "upstream_timeout", "The backend did not respond in time")
		default:
			p.logger.Warn("proxy_upstream_unavailable", "path", r.URL.Path, "err", err.Error())
			s.writeError(w, r, http.StatusBadGateway, "upstream_unavailable", "The backend is unavailable")
		}
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, maxErrorBodyBytes))
		if route.nodeErrors {
			if se := mapNodeError(resp.StatusCode, raw); se != nil {
				s.writeStoreError(w, r, se)
				return
			}
		}
		s.writeStoreError(w, r, mapUpstreamStatus(r.Method, r.URL.Path, resp.StatusCode, raw))
		return
	}
	for _, h := range forwardResponseHeaders {
		if v := resp.Header.Get(h); v != "" {
			w.Header().Set(h, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	if _, err := io.Copy(w, resp.Body); err != nil {
		p.logger.Warn("proxy_copy_failed", "path", r.URL.Path, "err", err.Error())
	}
}

// mapNodeError translates ssh-executor's onboarding and node errors, keeping
// their detail. nil means "use the generic mapping".
func mapNodeError(status int, body []byte) error {
	detail := upstreamDetail(body)
	if detail == "" {
		return nil
	}
	switch status {
	case http.StatusUnauthorized:
		// The gateway already accepted the caller's token: this is the
		// worker refusing the SSH login.
		return &StatusError{Status: http.StatusBadRequest, Code: "ssh_auth_failed", Message: detail}
	case http.StatusForbidden:
		return &StatusError{Status: http.StatusForbidden, Code: "host_not_allowed", Message: detail}
	case http.StatusBadGateway:
		return &StatusError{Status: http.StatusBadGateway, Code: "node_unreachable", Message: detail}
	}
	return nil
}

package server

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// UpstreamStore is the production Store: it calls ssh-executor's REST API
// (docs/API_CONTRACT.md §1-§2) over HTTP and maps its snake_case shapes onto
// the extension contract.
//
// Every request forwards the caller's bearer token (the user's JWT, see
// TokenFromContext) so ssh-executor scopes results to owner_id = jwt.sub.
// The gateway never uses a service identity of its own.
type UpstreamStore struct {
	base        string // e.g. http://ssh-executor:8011/api/v1/ssh
	client      *http.Client
	timeout     time.Duration
	logger      *slog.Logger
	concurrency int
}

// UpstreamConfig configures an UpstreamStore.
type UpstreamConfig struct {
	// BaseURL is ssh-executor's origin, e.g. http://ssh-executor:8011. The
	// /api/v1/ssh prefix is appended.
	BaseURL string
	// Timeout bounds each upstream call (default 15s).
	Timeout time.Duration
	// Client is the HTTP client (default: a client with no global timeout;
	// per-call contexts enforce Timeout).
	Client *http.Client
	Logger *slog.Logger
	// CapacityConcurrency bounds parallel /nodes/{id}/capacity calls (default 8).
	CapacityConcurrency int
}

const (
	upstreamAPIPrefix    = "/api/v1/ssh"
	maxUpstreamBodyBytes = 8 << 20
	maxDetailChars       = 500
)

// NewUpstreamStore validates cfg and builds an UpstreamStore.
func NewUpstreamStore(cfg UpstreamConfig) (*UpstreamStore, error) {
	u, err := url.Parse(strings.TrimSpace(cfg.BaseURL))
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("upstream URL %q must be an absolute http(s) URL", cfg.BaseURL)
	}
	if cfg.Timeout <= 0 {
		cfg.Timeout = 15 * time.Second
	}
	if cfg.Client == nil {
		cfg.Client = &http.Client{}
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.Default()
	}
	if cfg.CapacityConcurrency <= 0 {
		cfg.CapacityConcurrency = 8
	}
	base := strings.TrimRight(u.String(), "/")
	if !strings.HasSuffix(base, upstreamAPIPrefix) {
		base += upstreamAPIPrefix
	}
	return &UpstreamStore{
		base:        base,
		client:      cfg.Client,
		timeout:     cfg.Timeout,
		logger:      cfg.Logger,
		concurrency: cfg.CapacityConcurrency,
	}, nil
}

// -- upstream wire shapes (ssh-executor schemas.py) ---------------------------

type upNode struct {
	ID                  string            `json:"id"`
	Name                string            `json:"name"`
	Host                string            `json:"host"`
	OSType              *string           `json:"os_type"`
	OSInfo              *string           `json:"os_info"`
	DetectedCPU         int               `json:"detected_cpu"`
	DetectedMemoryGB    float64           `json:"detected_memory_gb"`
	DetectedGPU         int               `json:"detected_gpu"`
	GPUCount            int               `json:"gpu_count"`
	GPUModel            *string           `json:"gpu_model"`
	GPUMemoryGB         *float64          `json:"gpu_memory_gb"`
	GPURuntime          *string           `json:"gpu_runtime"`
	AllocGPUCount       *int              `json:"allocated_gpu_count"`
	AllocGPUMemoryGB    *float64          `json:"allocated_gpu_memory_gb"`
	AllocGPUFraction    *float64          `json:"allocated_gpu_fraction"`
	Runtimes            map[string]string `json:"runtimes"`
	ResourceEnforcement string            `json:"resource_enforcement"`
	AllocatedCPU        int               `json:"allocated_cpu"`
	AllocatedMemoryGB   float64           `json:"allocated_memory_gb"`
	Status              string            `json:"status"`
	HealthStatus        string            `json:"health_status"`
	LastHealthCheck     *upTime           `json:"last_health_check"`
}

type upNodeList struct {
	Items []upNode `json:"items"`
}

type upCapacity struct {
	CPUReserved      int     `json:"cpu_reserved"`
	MemoryReservedGB float64 `json:"memory_reserved_gb"`
	ActiveLeases     int     `json:"active_leases"`
}

type upRuntime struct {
	ID               string   `json:"id"`
	Name             string   `json:"name"`
	Filename         string   `json:"filename"`
	Kind             string   `json:"kind"`
	ExampleCode      string   `json:"example_code"`
	WindowsSupported bool     `json:"windows_supported"`
	AvailableNodeIDs []string `json:"available_node_ids"`
}

type upRuntimeList struct {
	Items []upRuntime `json:"items"`
}

type upBatch struct {
	ID             string  `json:"id"`
	Runtime        string  `json:"runtime"`
	Status         string  `json:"status"`
	Message        *string `json:"message"`
	Total          int     `json:"total"`
	Completed      int     `json:"completed"`
	Failed         int     `json:"failed"`
	Cancelled      int     `json:"cancelled"`
	CPUPerTask     int     `json:"cpu_per_task"`
	TimeoutSeconds int     `json:"timeout_seconds"`
	CreatedAt      upTime  `json:"created_at"`
	FinishedAt     *upTime `json:"finished_at"`
}

type upBatchList struct {
	Items []upBatch `json:"items"`
}

type upBatchCreate struct {
	Code            string   `json:"code"`
	Inputs          []string `json:"inputs,omitempty"`
	NodeIDs         []string `json:"node_ids,omitempty"`
	Runtime         string   `json:"runtime"`
	CustomCommand   string   `json:"custom_command,omitempty"`
	CustomFilename  string   `json:"custom_filename,omitempty"`
	CPUPerTask      int      `json:"cpu_per_task,omitempty"`
	TimeoutSeconds  int      `json:"timeout_seconds,omitempty"`
	ExecutionPolicy string   `json:"execution_policy,omitempty"`
}

// upNodeUpdate is ssh-executor's NodeUpdateRequest (nil = unchanged).
type upNodeUpdate struct {
	AllocatedCPU         *int     `json:"allocated_cpu,omitempty"`
	AllocatedMemoryGB    *float64 `json:"allocated_memory_gb,omitempty"`
	AllocatedGPUCount    *int     `json:"allocated_gpu_count,omitempty"`
	AllocatedGPUMemoryGB *float64 `json:"allocated_gpu_memory_gb,omitempty"`
	Status               *string  `json:"status,omitempty"`
}

// upTime accepts the ISO-8601 variants FastAPI/pydantic emit (with or without
// a zone; naive values are treated as UTC).
type upTime struct{ time.Time }

var upTimeLayouts = []string{
	time.RFC3339Nano,
	"2006-01-02T15:04:05.999999999",
	"2006-01-02 15:04:05.999999999Z07:00",
	"2006-01-02 15:04:05.999999999",
}

func (t *upTime) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err != nil {
		return err
	}
	for _, layout := range upTimeLayouts {
		if v, err := time.Parse(layout, s); err == nil {
			t.Time = v.UTC()
			return nil
		}
	}
	return fmt.Errorf("unrecognised timestamp %q", s)
}

func (t *upTime) ptr() *time.Time {
	if t == nil || t.IsZero() {
		return nil
	}
	v := t.Time
	return &v
}

// -- mapping --------------------------------------------------------------

func mapNode(n upNode) ComputeNode {
	out := ComputeNode{
		ID:              n.ID,
		Name:            n.Name,
		Host:            n.Host,
		Status:          mapNodeStatus(n.Status, n.HealthStatus),
		Runtimes:        n.Runtimes,
		LastHealthCheck: n.LastHealthCheck.ptr(),
	}
	if n.OSType != nil {
		out.OSType = *n.OSType
	}
	if n.OSInfo != nil {
		out.OSVersion = *n.OSInfo
	}
	// Detected values are 0 when detection never ran: report them as unknown
	// rather than as a real zero.
	if n.DetectedCPU > 0 {
		out.CPUTotal = intPtr(n.DetectedCPU)
		gpus := n.GPUCount
		if n.DetectedGPU > gpus {
			gpus = n.DetectedGPU
		}
		out.GPUCount = intPtr(gpus)
	}
	if n.DetectedMemoryGB > 0 {
		out.MemoryTotalGB = floatPtr(n.DetectedMemoryGB)
	}
	out.CPUAllocatable = intPtr(n.AllocatedCPU)
	out.MemoryAllocatableGB = floatPtr(n.AllocatedMemoryGB)
	if n.GPUModel != nil {
		out.GPUModel = *n.GPUModel
	}
	if n.GPUMemoryGB != nil && *n.GPUMemoryGB > 0 {
		out.GPUMemoryTotalGB = floatPtr(*n.GPUMemoryGB)
	}
	if n.GPURuntime != nil {
		out.GPURuntime = *n.GPURuntime
	}
	out.GPUAllocatableCount = n.AllocGPUCount
	out.GPUMemoryAllocatableGB = n.AllocGPUMemoryGB
	out.GPUFraction = n.AllocGPUFraction
	switch n.ResourceEnforcement {
	case "strict", "best_effort":
		out.ResourceEnforcement = n.ResourceEnforcement
	}
	return out
}

func applyCapacity(n *ComputeNode, c upCapacity) {
	n.CPUReserved = intPtr(c.CPUReserved)
	n.MemoryReservedGB = floatPtr(c.MemoryReservedGB)
	n.ActiveTasks = c.ActiveLeases
}

// mapNodeStatus folds ssh-executor's status + health_status into the
// extension's node states. Unknown values become "unknown", never "online".
func mapNodeStatus(status, health string) string {
	if strings.EqualFold(health, "unreachable") {
		return "unreachable"
	}
	switch s := strings.ToLower(status); s {
	case "online", "busy", "degraded", "offline", "unreachable", "draining":
		return s
	}
	return "unknown"
}

func mapBatch(b upBatch) Execution {
	e := Execution{
		ID:             b.ID,
		Runtime:        b.Runtime,
		Status:         b.Status,
		Total:          b.Total,
		Completed:      b.Completed,
		Failed:         b.Failed,
		Cancelled:      b.Cancelled,
		CPUPerTask:     b.CPUPerTask,
		TimeoutSeconds: b.TimeoutSeconds,
		CreatedAt:      b.CreatedAt.Time,
		EndedAt:        b.FinishedAt.ptr(),
	}
	if b.Message != nil {
		e.Message = *b.Message
	}
	return e
}

func mapRuntime(r upRuntime) Runtime {
	ids := r.AvailableNodeIDs
	if ids == nil {
		ids = []string{}
	}
	return Runtime{
		ID:               r.ID,
		Name:             r.Name,
		Filename:         r.Filename,
		Kind:             r.Kind,
		ExampleCode:      r.ExampleCode,
		WindowsSupported: r.WindowsSupported,
		AvailableNodeIDs: ids,
	}
}

// -- Store implementation -----------------------------------------------------

func (s *UpstreamStore) ListNodes(ctx context.Context) ([]ComputeNode, error) {
	var list upNodeList
	if err := s.do(ctx, http.MethodGet, "/nodes", nil, &list); err != nil {
		return nil, err
	}
	nodes := make([]ComputeNode, len(list.Items))
	for i, n := range list.Items {
		nodes[i] = mapNode(n)
	}
	s.fillCapacity(ctx, nodes)
	return nodes, nil
}

func (s *UpstreamStore) GetNode(ctx context.Context, id string) (ComputeNode, error) {
	if id == "" {
		return ComputeNode{}, ErrNotFound
	}
	var n upNode
	if err := s.do(ctx, http.MethodGet, "/nodes/"+url.PathEscape(id), nil, &n); err != nil {
		return ComputeNode{}, err
	}
	nodes := []ComputeNode{mapNode(n)}
	s.fillCapacity(ctx, nodes)
	return nodes[0], nil
}

// UpdateNodeSlice forwards a slice change to PATCH /nodes/{id}.
func (s *UpstreamStore) UpdateNodeSlice(ctx context.Context, id string, req NodeSliceRequest) (ComputeNode, error) {
	if id == "" {
		return ComputeNode{}, ErrNotFound
	}
	body := upNodeUpdate{
		AllocatedCPU:         req.CPUAllocatable,
		AllocatedMemoryGB:    req.MemoryAllocatableGB,
		AllocatedGPUCount:    req.GPUAllocatableCount,
		AllocatedGPUMemoryGB: req.GPUMemoryAllocatableGB,
		Status:               req.Status,
	}
	var n upNode
	if err := s.do(ctx, http.MethodPatch, "/nodes/"+url.PathEscape(id), body, &n); err != nil {
		return ComputeNode{}, err
	}
	nodes := []ComputeNode{mapNode(n)}
	s.fillCapacity(ctx, nodes)
	return nodes[0], nil
}

// fillCapacity adds live lease usage to each node, in parallel. A failed
// capacity lookup leaves the reserved fields nil ("Unknown" in the UI) rather
// than failing the whole listing.
func (s *UpstreamStore) fillCapacity(ctx context.Context, nodes []ComputeNode) {
	sem := make(chan struct{}, s.concurrency)
	var wg sync.WaitGroup
	for i := range nodes {
		wg.Add(1)
		sem <- struct{}{}
		go func(n *ComputeNode) {
			defer wg.Done()
			defer func() { <-sem }()
			var c upCapacity
			if err := s.do(ctx, http.MethodGet, "/nodes/"+url.PathEscape(n.ID)+"/capacity", nil, &c); err != nil {
				s.logger.Warn("upstream_capacity_unavailable", "node_id", n.ID, "err", err.Error())
				return
			}
			applyCapacity(n, c)
		}(&nodes[i])
	}
	wg.Wait()
}

func (s *UpstreamStore) ClusterStatus(ctx context.Context) (ClusterStatus, error) {
	nodes, err := s.ListNodes(ctx)
	if err != nil {
		return ClusterStatus{}, err
	}
	execs, err := s.ListExecutions(ctx)
	if err != nil {
		return ClusterStatus{}, err
	}
	return aggregateCluster(nodes, execs), nil
}

func (s *UpstreamStore) ListRuntimes(ctx context.Context) ([]Runtime, error) {
	var list upRuntimeList
	if err := s.do(ctx, http.MethodGet, "/runtimes", nil, &list); err != nil {
		return nil, err
	}
	out := make([]Runtime, len(list.Items))
	for i, r := range list.Items {
		out[i] = mapRuntime(r)
	}
	return out, nil
}

// ListExecutions returns the caller's latest batches (ssh-executor returns the
// most recent 20).
func (s *UpstreamStore) ListExecutions(ctx context.Context) ([]Execution, error) {
	var list upBatchList
	if err := s.do(ctx, http.MethodGet, "/batches", nil, &list); err != nil {
		return nil, err
	}
	out := make([]Execution, len(list.Items))
	for i, b := range list.Items {
		out[i] = mapBatch(b)
	}
	return out, nil
}

func (s *UpstreamStore) GetExecution(ctx context.Context, id string) (Execution, error) {
	if id == "" {
		return Execution{}, ErrNotFound
	}
	var b upBatch
	if err := s.do(ctx, http.MethodGet, "/batches/"+url.PathEscape(id), nil, &b); err != nil {
		return Execution{}, err
	}
	return mapBatch(b), nil
}

func (s *UpstreamStore) CreateExecution(ctx context.Context, req ExecutionRequest) (Execution, error) {
	body := upBatchCreate{
		Code:            req.Code,
		Inputs:          req.Inputs,
		NodeIDs:         req.NodeIDs,
		Runtime:         req.Runtime,
		CustomCommand:   req.CustomCommand,
		CustomFilename:  req.CustomFilename,
		CPUPerTask:      req.CPUPerTask,
		TimeoutSeconds:  req.TimeoutSeconds,
		ExecutionPolicy: req.ExecutionPolicy,
	}
	var b upBatch
	if err := s.do(ctx, http.MethodPost, "/batches", body, &b); err != nil {
		return Execution{}, err
	}
	return mapBatch(b), nil
}

func (s *UpstreamStore) CancelExecution(ctx context.Context, id string) (Execution, error) {
	if id == "" {
		return Execution{}, ErrNotFound
	}
	var b upBatch
	if err := s.do(ctx, http.MethodPost, "/batches/"+url.PathEscape(id)+"/cancel", nil, &b); err != nil {
		return Execution{}, err
	}
	return mapBatch(b), nil
}

// -- transport ----------------------------------------------------------------

// do performs one upstream call with a bounded timeout, forwarding the
// caller's bearer token and request id, and decodes a 2xx JSON body into out.
// Every failure comes back as a *StatusError the handlers can render.
func (s *UpstreamStore) do(ctx context.Context, method, path string, in, out any) error {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()

	var body io.Reader
	if in != nil {
		b, err := json.Marshal(in)
		if err != nil {
			return &StatusError{Status: http.StatusInternalServerError, Code: "internal", Message: "Internal error", Err: err}
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, s.base+path, body)
	if err != nil {
		return &StatusError{Status: http.StatusInternalServerError, Code: "internal", Message: "Internal error", Err: err}
	}
	req.Header.Set("Accept", "application/json")
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if tok := TokenFromContext(ctx); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	if id, _ := ctx.Value(requestIDKey).(string); id != "" {
		req.Header.Set("X-Request-Id", id)
	}

	resp, err := s.client.Do(req)
	if err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return &StatusError{Status: http.StatusGatewayTimeout, Code: "upstream_timeout",
				Message: "The backend did not respond in time", Err: err}
		}
		return &StatusError{Status: http.StatusBadGateway, Code: "upstream_unavailable",
			Message: "The backend is unavailable", Err: err}
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxUpstreamBodyBytes))
	if err != nil {
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return &StatusError{Status: http.StatusGatewayTimeout, Code: "upstream_timeout",
				Message: "The backend did not respond in time", Err: err}
		}
		return &StatusError{Status: http.StatusBadGateway, Code: "upstream_unavailable",
			Message: "The backend is unavailable", Err: err}
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return mapUpstreamStatus(method, path, resp.StatusCode, raw)
	}
	if out == nil || len(raw) == 0 {
		return nil
	}
	if err := json.Unmarshal(raw, out); err != nil {
		return &StatusError{Status: http.StatusBadGateway, Code: "upstream_bad_response",
			Message: "The backend returned an unexpected response", Err: err}
	}
	return nil
}

// mapUpstreamStatus converts an ssh-executor error response into the status
// the extension should see. Validation/conflict details are user-facing text
// from ssh-executor and are passed through; 5xx bodies are never exposed.
func mapUpstreamStatus(method, path string, status int, body []byte) error {
	cause := fmt.Errorf("upstream %s %s -> %d", method, path, status)
	detail := upstreamDetail(body)
	withDetail := func(fallback string) string {
		if detail != "" {
			return detail
		}
		return fallback
	}
	switch {
	case status == http.StatusBadRequest || status == http.StatusUnprocessableEntity:
		return &StatusError{Status: http.StatusBadRequest, Code: "invalid_request",
			Message: withDetail("The backend rejected the request"), Err: cause}
	case status == http.StatusUnauthorized:
		return &StatusError{Status: http.StatusUnauthorized, Code: "invalid_token",
			Message: "Authentication failed", Err: cause}
	case status == http.StatusForbidden:
		return &StatusError{Status: http.StatusForbidden, Code: "forbidden",
			Message: "Not allowed", Err: cause}
	case status == http.StatusNotFound:
		return &StatusError{Status: http.StatusNotFound, Code: "not_found",
			Message: "Resource not found", Err: fmt.Errorf("%w: %v", ErrNotFound, cause)}
	case status == http.StatusConflict:
		return &StatusError{Status: http.StatusConflict, Code: "conflict",
			Message: withDetail("The request conflicts with the current state"), Err: cause}
	case status == http.StatusRequestEntityTooLarge:
		return &StatusError{Status: http.StatusRequestEntityTooLarge, Code: "too_large",
			Message: withDetail("The request is too large"), Err: cause}
	case status == http.StatusTooManyRequests:
		return &StatusError{Status: http.StatusTooManyRequests, Code: "rate_limited",
			Message: "Too many requests; try again shortly", Err: cause}
	case status == http.StatusNotImplemented:
		return &StatusError{Status: http.StatusNotImplemented, Code: "not_implemented",
			Message: "This capability is not supported by the connected backend yet",
			Err:     fmt.Errorf("%w: %v", ErrNotImplemented, cause)}
	case status == http.StatusServiceUnavailable && detail != "":
		// e.g. a dataset kept in HDFS while HDFS is not running; the
		// message says how to fix it.
		return &StatusError{Status: http.StatusServiceUnavailable, Code: "unavailable",
			Message: detail, Err: cause}
	case status == http.StatusInsufficientStorage:
		return &StatusError{Status: http.StatusInsufficientStorage, Code: "insufficient_storage",
			Message: withDetail("The master is out of disk space for datasets"), Err: cause}
	case status == http.StatusGatewayTimeout:
		return &StatusError{Status: http.StatusGatewayTimeout, Code: "upstream_timeout",
			Message: "The backend did not respond in time", Err: cause}
	case status >= 500:
		return &StatusError{Status: http.StatusBadGateway, Code: "upstream_error",
			Message: "The backend reported an error", Err: cause}
	default:
		return &StatusError{Status: http.StatusBadGateway, Code: "upstream_error",
			Message: "The backend returned an unexpected status", Err: cause}
	}
}

// upstreamDetail extracts FastAPI's `detail` (a string, or a list of
// {loc, msg} validation errors) or `message`, truncated.
func upstreamDetail(body []byte) string {
	var env struct {
		Detail  json.RawMessage `json:"detail"`
		Message string          `json:"message"`
	}
	if json.Unmarshal(body, &env) != nil {
		return ""
	}
	var msg string
	var s string
	var list []struct {
		Loc []any  `json:"loc"`
		Msg string `json:"msg"`
	}
	switch {
	case len(env.Detail) > 0 && json.Unmarshal(env.Detail, &s) == nil:
		msg = s
	case len(env.Detail) > 0 && json.Unmarshal(env.Detail, &list) == nil:
		parts := make([]string, 0, len(list))
		for _, e := range list {
			field := ""
			if len(e.Loc) > 0 {
				field = fmt.Sprint(e.Loc[len(e.Loc)-1])
			}
			if field != "" {
				parts = append(parts, field+": "+e.Msg)
			} else {
				parts = append(parts, e.Msg)
			}
		}
		msg = strings.Join(parts, "; ")
	default:
		msg = env.Message
	}
	msg = strings.TrimSpace(msg)
	if r := []rune(msg); len(r) > maxDetailChars {
		msg = string(r[:maxDetailChars]) + "…"
	}
	return msg
}

func intPtr(v int) *int           { return &v }
func floatPtr(v float64) *float64 { return &v }

package server

import (
	"encoding/json"
	"net/http"
	"regexp"
	"strings"
)

var (
	// runtimeIDPattern is a syntactic check only; whether the runtime exists
	// (GET /runtimes) and is detected on a selected node is ssh-executor's call.
	runtimeIDPattern      = regexp.MustCompile(`^[a-z0-9][a-z0-9_.+-]{0,31}$`)
	customFilenamePattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)

	executionPolicies = map[string]bool{"safe_background": true, "balanced": true, "max_available": true}
)

func (s *Server) handleHealth(w http.ResponseWriter, _ *http.Request) {
	s.writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) handleClusterStatus(w http.ResponseWriter, r *http.Request) {
	cs, err := s.cfg.Store.ClusterStatus(r.Context())
	if err != nil {
		s.writeStoreError(w, r, err)
		return
	}
	s.writeJSON(w, http.StatusOK, cs)
}

func (s *Server) handleListNodes(w http.ResponseWriter, r *http.Request) {
	nodes, err := s.cfg.Store.ListNodes(r.Context())
	if err != nil {
		s.writeStoreError(w, r, err)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"items": nodes, "total": len(nodes)})
}

func (s *Server) handleGetNode(w http.ResponseWriter, r *http.Request) {
	node, err := s.cfg.Store.GetNode(r.Context(), r.PathValue("id"))
	if err != nil {
		s.writeStoreError(w, r, err)
		return
	}
	s.writeJSON(w, http.StatusOK, node)
}

func (s *Server) handleListExecutions(w http.ResponseWriter, r *http.Request) {
	execs, err := s.cfg.Store.ListExecutions(r.Context())
	if err != nil {
		s.writeStoreError(w, r, err)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"items": execs, "total": len(execs)})
}

func (s *Server) handleGetExecution(w http.ResponseWriter, r *http.Request) {
	exec, err := s.cfg.Store.GetExecution(r.Context(), r.PathValue("id"))
	if err != nil {
		s.writeStoreError(w, r, err)
		return
	}
	s.writeJSON(w, http.StatusOK, exec)
}

func (s *Server) handleCreateExecution(w http.ResponseWriter, r *http.Request) {
	var req ExecutionRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		s.writeError(w, r, http.StatusBadRequest, "invalid_body", "Request body is not valid JSON")
		return
	}
	if code, msg := validateExecutionRequest(&req); code != "" {
		s.writeError(w, r, http.StatusBadRequest, code, msg)
		return
	}
	exec, err := s.cfg.Store.CreateExecution(r.Context(), req)
	if err != nil {
		s.writeStoreError(w, r, err)
		return
	}
	s.writeJSON(w, http.StatusCreated, exec)
}

func (s *Server) handleUpdateNodeSlice(w http.ResponseWriter, r *http.Request) {
	var req NodeSliceRequest
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<16))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&req); err != nil {
		s.writeError(w, r, http.StatusBadRequest, "invalid_body", "Request body is not valid JSON")
		return
	}
	if code, msg := validateNodeSliceRequest(req); code != "" {
		s.writeError(w, r, http.StatusBadRequest, code, msg)
		return
	}
	node, err := s.cfg.Store.UpdateNodeSlice(r.Context(), r.PathValue("id"), req)
	if err != nil {
		s.writeStoreError(w, r, err)
		return
	}
	s.writeJSON(w, http.StatusOK, node)
}

// validateNodeSliceRequest rejects empty or negative slices; upper bounds
// (<= detected) are enforced by ssh-executor.
func validateNodeSliceRequest(req NodeSliceRequest) (code, msg string) {
	switch {
	case req.CPUAllocatable == nil && req.MemoryAllocatableGB == nil &&
		req.GPUAllocatableCount == nil && req.GPUMemoryAllocatableGB == nil && req.Status == nil &&
		req.EnvCacheGB == nil:
		return "invalid_slice", "at least one slice field is required"
	case req.Status != nil && *req.Status != "online" && *req.Status != "draining":
		return "invalid_status", "status must be online or draining"
	case req.CPUAllocatable != nil && *req.CPUAllocatable < 1:
		return "invalid_cpu", "cpuAllocatable must be >= 1"
	case req.MemoryAllocatableGB != nil && *req.MemoryAllocatableGB <= 0:
		return "invalid_memory", "memoryAllocatableGb must be > 0"
	case req.GPUAllocatableCount != nil && *req.GPUAllocatableCount < 0:
		return "invalid_gpu", "gpuAllocatableCount must be >= 0"
	case req.GPUMemoryAllocatableGB != nil && *req.GPUMemoryAllocatableGB < 0:
		return "invalid_gpu", "gpuMemoryAllocatableGb must be >= 0"
	case req.EnvCacheGB != nil && (*req.EnvCacheGB < 0 || *req.EnvCacheGB > 10000):
		return "invalid_env_cache", "envCacheGb must be between 0 and 10000"
	}
	return "", ""
}

func (s *Server) handleCancelExecution(w http.ResponseWriter, r *http.Request) {
	exec, err := s.cfg.Store.CancelExecution(r.Context(), r.PathValue("id"))
	if err != nil {
		s.writeStoreError(w, r, err)
		return
	}
	s.writeJSON(w, http.StatusOK, exec)
}

func (s *Server) handleListRuntimes(w http.ResponseWriter, r *http.Request) {
	rts, err := s.cfg.Store.ListRuntimes(r.Context())
	if err != nil {
		s.writeStoreError(w, r, err)
		return
	}
	s.writeJSON(w, http.StatusOK, map[string]any{"items": rts, "total": len(rts)})
}

// validateExecutionRequest does cheap structural checks so obviously bad
// requests fail fast with a clear message. It returns ("", "") when valid.
// Any runtime id is accepted here; ssh-executor rejects unknown runtimes and
// runtimes not detected on any selected node (surfaced as 400 with its detail).
func validateExecutionRequest(req *ExecutionRequest) (code, msg string) {
	req.Runtime = strings.TrimSpace(req.Runtime)
	if req.Project != nil {
		return validateProject(req)
	}
	switch {
	case req.Runtime == "project":
		return "invalid_project", "runtime project needs a project (bundleId and command)"
	case !runtimeIDPattern.MatchString(req.Runtime):
		return "invalid_runtime", "runtime must be a runtime id from GET /runtimes (e.g. python, bash, node, go, custom)"
	case strings.TrimSpace(req.Code) == "":
		return "invalid_code", "code must not be empty"
	case req.CPUPerTask < 0:
		return "invalid_cpu", "cpuPerTask must be >= 1 (or omitted)"
	case req.TimeoutSeconds < 0:
		return "invalid_timeout", "timeoutSeconds must be >= 1 (or omitted)"
	case req.ExecutionPolicy != "" && !executionPolicies[req.ExecutionPolicy]:
		return "invalid_execution_policy", "executionPolicy must be safe_background, balanced or max_available"
	}
	for _, id := range req.NodeIDs {
		if strings.TrimSpace(id) == "" {
			return "invalid_node_ids", "nodeIds must not contain empty ids"
		}
	}
	if req.Runtime == "custom" {
		if !strings.Contains(req.CustomCommand, "{file}") {
			return "invalid_custom_command", "customCommand must contain {file} for the custom runtime"
		}
		if req.CustomFilename != "" && !customFilenamePattern.MatchString(req.CustomFilename) {
			return "invalid_custom_filename", "customFilename must match ^[A-Za-z0-9._-]{1,128}$"
		}
	} else if req.CustomCommand != "" || req.CustomFilename != "" {
		return "invalid_custom_command", "customCommand/customFilename are only allowed with runtime custom"
	}
	return "", ""
}

var bundleIDPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

// validateProject checks the parts the gateway can; paths are checked by
// ssh-executor, which owns the rules.
func validateProject(req *ExecutionRequest) (code, msg string) {
	p := req.Project
	switch {
	case req.Runtime != "" && req.Runtime != "project":
		return "invalid_project", "runtime must be project (or omitted) when a project is given"
	case !bundleIDPattern.MatchString(p.BundleID):
		return "invalid_project", "project.bundleId must be the sha256 returned by POST /bundles"
	case strings.TrimSpace(p.Command) == "":
		return "invalid_project", "project.command must not be empty"
	case req.CustomCommand != "" || req.CustomFilename != "":
		return "invalid_custom_command", "customCommand/customFilename are only allowed with runtime custom"
	case req.CPUPerTask < 0:
		return "invalid_cpu", "cpuPerTask must be >= 1 (or omitted)"
	case req.TimeoutSeconds < 0:
		return "invalid_timeout", "timeoutSeconds must be >= 1 (or omitted)"
	case req.ExecutionPolicy != "" && !executionPolicies[req.ExecutionPolicy]:
		return "invalid_execution_policy", "executionPolicy must be safe_background, balanced or max_available"
	}
	for _, d := range p.Datasets {
		if strings.TrimSpace(d.DatasetID) == "" {
			return "invalid_project", "project.datasets[].datasetId must not be empty"
		}
	}
	req.Runtime = "project"
	return "", ""
}

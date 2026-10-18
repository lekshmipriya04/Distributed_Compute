// Package server implements the extension-facing control-plane HTTP service.
//
// This is the Go backend the Distributed Compute VS Code extension talks to. It
// adapts the platform's ssh-executor REST API (/api/v1/ssh/...) behind the
// stable camelCase JSON contract the extension expects; see UpstreamStore.
// Data is served through the Store interface; no values are fabricated in any
// wiring (MemStore, used for tests and local dev, starts empty).
package server

import "time"

// ComputeNode is the extension-facing view of an ssh-executor worker node.
// Pointer fields are nil when the backend has not reported a value (shown as
// "Unknown" in the UI) rather than a misleading zero.
//
// Capacity concepts are kept distinct: CPUTotal/MemoryTotalGB are what was
// detected on the machine, CPUAllocatable/MemoryAllocatableGB are what the
// owner lent to the platform, and CPUReserved/MemoryReservedGB are currently
// held by active leases.
type ComputeNode struct {
	ID                  string   `json:"id"`
	Name                string   `json:"name"`
	Host                string   `json:"host,omitempty"`
	OSType              string   `json:"osType,omitempty"`
	OSVersion           string   `json:"osVersion,omitempty"`
	Status              string   `json:"status"` // online|busy|degraded|offline|unreachable|draining|unknown
	CPUTotal            *int     `json:"cpuTotal,omitempty"`
	CPUAllocatable      *int     `json:"cpuAllocatable,omitempty"`
	CPUReserved         *int     `json:"cpuReserved,omitempty"`
	MemoryTotalGB       *float64 `json:"memoryTotalGb,omitempty"`
	MemoryAllocatableGB *float64 `json:"memoryAllocatableGb,omitempty"`
	MemoryReservedGB    *float64 `json:"memoryReservedGb,omitempty"`
	GPUCount            *int     `json:"gpuCount,omitempty"`
	GPUModel            string   `json:"gpuModel,omitempty"`
	GPUMemoryTotalGB    *float64 `json:"gpuMemoryTotalGb,omitempty"`
	// GPU slice lent to the platform (count / VRAM GB / fraction of VRAM).
	GPUAllocatableCount    *int     `json:"gpuAllocatableCount,omitempty"`
	GPUMemoryAllocatableGB *float64 `json:"gpuMemoryAllocatableGb,omitempty"`
	GPUFraction            *float64 `json:"gpuFraction,omitempty"`
	// GPURuntime is the detected GPU runtime (e.g. torch); empty when none was
	// found, in which case a GPU slice cannot be enforced on the worker.
	GPURuntime          string `json:"gpuRuntime,omitempty"`
	ResourceEnforcement string `json:"resourceEnforcement,omitempty"` // strict|best_effort
	// EnvCacheGB is the disk lent for cached project environments (0 = off).
	EnvCacheGB      *float64          `json:"envCacheGb,omitempty"`
	Runtimes        map[string]string `json:"runtimes,omitempty"` // runtime id -> detected version
	ActiveTasks     int               `json:"activeTasks"`
	LastHealthCheck *time.Time        `json:"lastHealthCheck,omitempty"`
}

// Execution is a submitted execution; it maps 1:1 to an ssh-executor batch
// (one task per input).
type Execution struct {
	ID        string `json:"id"`
	Runtime   string `json:"runtime"` // any runtime id from GET /runtimes (python, bash, node, go, ..., custom)
	Status    string `json:"status"`  // pending|running|cancelling|cancelled|completed|completed_with_errors|failed
	Message   string `json:"message,omitempty"`
	Total     int    `json:"total"`
	Completed int    `json:"completed"`
	Failed    int    `json:"failed"`
	Cancelled int    `json:"cancelled"`
	// CPUPerTask and TimeoutSeconds echo the accepted request; 0 = not reported.
	CPUPerTask     int        `json:"cpuPerTask,omitempty"`
	TimeoutSeconds int        `json:"timeoutSeconds,omitempty"`
	CreatedAt      time.Time  `json:"createdAt"`
	EndedAt        *time.Time `json:"endedAt,omitempty"`
	// Project is set for project runs (runtime "project").
	Project *ProjectSpec `json:"project,omitempty"`
}

// ProjectSpec runs a folder: BundleID is the sha256 of the .tar.gz uploaded
// with POST /bundles, Command runs inside the unpacked folder. It maps onto
// ssh-executor's ProjectSpec.
type ProjectSpec struct {
	BundleID     string           `json:"bundleId"`
	Command      string           `json:"command"`
	Requirements string           `json:"requirements,omitempty"` // pip requirements file in the project
	Outputs      []string         `json:"outputs,omitempty"`      // folders brought back; default ["outputs"]
	Datasets     []ProjectDataset `json:"datasets,omitempty"`
}

// ProjectDataset copies a dataset into the project before the command runs.
type ProjectDataset struct {
	DatasetID string `json:"datasetId"`
	Path      string `json:"path,omitempty"` // default data/<name>.<format>
}

// ExecutionRequest is the body for POST /executions. It maps onto the
// ssh-executor BatchCreateRequest. Runtime is any id listed by GET /runtimes;
// membership and per-node availability are validated by ssh-executor.
type ExecutionRequest struct {
	Runtime        string   `json:"runtime"`
	Code           string   `json:"code"`
	Inputs         []string `json:"inputs,omitempty"`         // one task per input (stdin); empty = one task
	NodeIDs        []string `json:"nodeIds,omitempty"`        // empty = any of the caller's online nodes
	CustomCommand  string   `json:"customCommand,omitempty"`  // runtime "custom" only; must contain {file}
	CustomFilename string   `json:"customFilename,omitempty"` // runtime "custom" only, e.g. main.zig
	CPUPerTask     int      `json:"cpuPerTask,omitempty"`
	TimeoutSeconds int      `json:"timeoutSeconds,omitempty"`
	// ExecutionPolicy is safe_background (default) | balanced | max_available.
	ExecutionPolicy string `json:"executionPolicy,omitempty"`
	// Project runs a folder instead of Code (runtime is then "project").
	Project *ProjectSpec `json:"project,omitempty"`
}

// NodeSliceRequest is the body for PATCH /nodes/{id}: change the resource
// slice lent to the platform. Nil fields are left unchanged; ssh-executor
// clamps/validates against detected values.
type NodeSliceRequest struct {
	CPUAllocatable         *int     `json:"cpuAllocatable,omitempty"`
	MemoryAllocatableGB    *float64 `json:"memoryAllocatableGb,omitempty"`
	GPUAllocatableCount    *int     `json:"gpuAllocatableCount,omitempty"`
	GPUMemoryAllocatableGB *float64 `json:"gpuMemoryAllocatableGb,omitempty"`
	// Status "draining" stops new work going to the node; "online" resumes it.
	Status *string `json:"status,omitempty"`
	// EnvCacheGB lends disk for cached project environments (0 turns it off).
	EnvCacheGB *float64 `json:"envCacheGb,omitempty"`
}

// Runtime is a language runtime the backend knows how to run. A runtime is
// usable only on nodes where it was detected (AvailableNodeIDs).
type Runtime struct {
	ID               string   `json:"id"`
	Name             string   `json:"name"`
	Filename         string   `json:"filename"`
	Kind             string   `json:"kind"` // interpreted|compiled|custom
	ExampleCode      string   `json:"exampleCode,omitempty"`
	WindowsSupported bool     `json:"windowsSupported"`
	AvailableNodeIDs []string `json:"availableNodeIds"`
}

// ClusterStatus is the aggregate shown in the cluster dashboard. The capacity
// concepts are kept distinct (detected total vs allocatable vs reserved)
// rather than conflated.
type ClusterStatus struct {
	NodesTotal          int     `json:"nodesTotal"`
	NodesOnline         int     `json:"nodesOnline"`
	CPUTotal            int     `json:"cpuTotal"`
	CPUAllocatable      int     `json:"cpuAllocatable"`
	CPUReserved         int     `json:"cpuReserved"`
	MemoryTotalGB       float64 `json:"memoryTotalGb"`
	MemoryAllocatableGB float64 `json:"memoryAllocatableGb"`
	MemoryReservedGB    float64 `json:"memoryReservedGb"`
	JobsRunning         int     `json:"jobsRunning"`
}

// apiError is the uniform error envelope.
type apiError struct {
	Error     string `json:"error"`
	Code      string `json:"code"`
	RequestID string `json:"requestId,omitempty"`
}

// LocalSubject is the single local user. It matches the Python services'
// anonymous development user, so the extension and the web dashboard see the
// same nodes and jobs.
const LocalSubject = "dev-user"

type localTokenResponse struct {
	Token     string `json:"token"`
	Subject   string `json:"subject"`
	ExpiresAt string `json:"expiresAt"`
}

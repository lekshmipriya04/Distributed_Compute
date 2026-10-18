package server

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

var testBundleID = strings.Repeat("ab", 32)

func TestCreateProjectValidation(t *testing.T) {
	s := newTestServer()
	ok := ProjectSpec{BundleID: testBundleID, Command: "python train.py"}
	cases := []struct {
		name string
		req  ExecutionRequest
		code string
	}{
		{"runtime project without project", ExecutionRequest{Runtime: "project", Code: "x"}, "invalid_project"},
		{"other runtime with project", ExecutionRequest{Runtime: "python", Project: &ok}, "invalid_project"},
		{"bad bundle id", ExecutionRequest{Project: &ProjectSpec{BundleID: "abc", Command: "x"}}, "invalid_project"},
		{"blank command", ExecutionRequest{Project: &ProjectSpec{BundleID: testBundleID, Command: "  "}}, "invalid_project"},
		{"custom fields", ExecutionRequest{Project: &ok, CustomCommand: "x {file}"}, "invalid_custom_command"},
		{"blank dataset", ExecutionRequest{Project: &ProjectSpec{BundleID: testBundleID, Command: "x",
			Datasets: []ProjectDataset{{DatasetID: " "}}}}, "invalid_project"},
		{"negative cpu", ExecutionRequest{Project: &ok, CPUPerTask: -1}, "invalid_cpu"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := do(t, s, "POST", "/executions", "tok", tc.req)
			if w.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400 (%s)", w.Code, w.Body.String())
			}
			if got := decodeBody[apiError](t, w).Code; got != tc.code {
				t.Fatalf("code = %q, want %q", got, tc.code)
			}
		})
	}
	// A project needs no code, and the runtime becomes "project".
	w := do(t, s, "POST", "/executions", "tok", ExecutionRequest{Project: &ok})
	if w.Code != http.StatusCreated || decodeBody[Execution](t, w).Runtime != "project" {
		t.Fatalf("project create = %d %s", w.Code, w.Body.String())
	}
}

func TestUpstreamCreateProjectMapsBody(t *testing.T) {
	fake := &fakeExecutor{}
	s, _ := newUpstreamGateway(t, fake, time.Second)
	tok := userToken(t, "alice")
	w := do(t, s, "POST", "/executions", tok, ExecutionRequest{
		Inputs: []string{"a"},
		Project: &ProjectSpec{BundleID: testBundleID, Command: "python train.py", Requirements: "requirements.txt",
			Outputs: []string{"models"}, Datasets: []ProjectDataset{{DatasetID: "d1", Path: "data/t.csv"}, {DatasetID: "d2"}}},
	})
	if w.Code != http.StatusCreated {
		t.Fatalf("create = %d: %s", w.Code, w.Body.String())
	}
	reqs := fake.all()
	b := reqs[len(reqs)-1].Body
	p, _ := b["project"].(map[string]any)
	if b["runtime"] != "project" || p["bundle_id"] != testBundleID || p["command"] != "python train.py" ||
		p["requirements"] != "requirements.txt" {
		t.Fatalf("upstream body = %+v", b)
	}
	ds, _ := p["datasets"].([]any)
	first, _ := ds[0].(map[string]any)
	second, _ := ds[1].(map[string]any)
	if len(ds) != 2 || first["dataset_id"] != "d1" || first["path"] != "data/t.csv" {
		t.Fatalf("datasets = %+v", p["datasets"])
	}
	if _, has := second["path"]; has {
		t.Fatalf("an empty path must be omitted so the default applies: %+v", second)
	}
	if _, has := p["requirements"]; !has {
		t.Fatal("requirements missing")
	}
}

func TestMapBatchProject(t *testing.T) {
	req := "requirements.txt"
	path := "data/x.csv"
	e := mapBatch(upBatch{ID: "b1", Runtime: "project", Project: &upProject{
		BundleID: testBundleID, Command: "make", Requirements: &req, Outputs: []string{"outputs"},
		Datasets: []upProjectData{{DatasetID: "d1", Path: &path}},
	}})
	if e.Project == nil || e.Project.Command != "make" || e.Project.Requirements != req ||
		e.Project.Datasets[0].Path != path {
		t.Fatalf("mapped = %+v", e.Project)
	}
	if mapBatch(upBatch{ID: "b2", Runtime: "python"}).Project != nil {
		t.Fatal("a code run has no project")
	}
}

func TestProxyProjectRoutes(t *testing.T) {
	backend, calls := fakeBackend(t, func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/outputs") {
			w.Header().Set("Content-Type", "application/gzip")
			w.Header().Set("Content-Disposition", `attachment; filename="outputs-1.tar.gz"`)
			_, _ = w.Write([]byte("tgz"))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"id":"x","size_bytes":3}`))
	})
	s := proxyServer(t, backend.URL, "")

	req := httptest.NewRequest("POST", "/bundles", bytes.NewReader([]byte{0x1f, 0x8b, 0x08}))
	req.Header.Set("Authorization", "Bearer t")
	req.Header.Set("Content-Type", "application/gzip")
	w := httptest.NewRecorder()
	s.Handler().ServeHTTP(w, req)
	if w.Code != http.StatusCreated {
		t.Fatalf("POST /bundles = %d %s", w.Code, w.Body.String())
	}
	got := (*calls)[0]
	if got.path != "/api/v1/ssh/bundles" || got.contentType != "application/gzip" || !bytes.Equal(got.body, []byte{0x1f, 0x8b, 0x08}) {
		t.Fatalf("bundle forwarded as %+v", got)
	}

	do(t, s, "GET", "/bundles/"+testBundleID, "t", nil)
	if (*calls)[1].path != "/api/v1/ssh/bundles/"+testBundleID {
		t.Fatalf("bundle check forwarded as %s", (*calls)[1].path)
	}

	w = do(t, s, "GET", "/executions/e1/tasks/t9/outputs", "t", nil)
	if (*calls)[2].path != "/api/v1/ssh/batches/e1/tasks/t9/outputs" {
		t.Fatalf("outputs forwarded as %s", (*calls)[2].path)
	}
	if w.Body.String() != "tgz" || w.Header().Get("Content-Type") != "application/gzip" ||
		!strings.Contains(w.Header().Get("Content-Disposition"), "outputs-1.tar.gz") {
		t.Fatalf("outputs response = %d %q %v", w.Code, w.Body.String(), w.Header())
	}
}

func TestNodeSliceAcceptsEnvCache(t *testing.T) {
	gb := 20.0
	if code, _ := validateNodeSliceRequest(NodeSliceRequest{EnvCacheGB: &gb}); code != "" {
		t.Fatalf("envCacheGb alone must be accepted, got %s", code)
	}
	bad := -1.0
	if code, _ := validateNodeSliceRequest(NodeSliceRequest{EnvCacheGB: &bad}); code != "invalid_env_cache" {
		t.Fatalf("negative envCacheGb = %q", code)
	}
}

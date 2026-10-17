"""Training engines: parameter averaging, custom models, Spark MLlib and Ray.

Spark and Ray tests run for real when pyspark, ray and Java are installed
(the master image built with WITH_SPARK_RAY=true) and are skipped otherwise.

    python -m pytest tests/unit/services/test_ml_engines.py -q
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import importlib
import importlib.util
import json
import os
import random
import shutil
import subprocess
import sys
import types
import uuid
import zipfile

import pytest

HAS_SPARK = importlib.util.find_spec("pyspark") is not None and shutil.which("java") is not None
HAS_RAY = importlib.util.find_spec("ray") is not None
needs_spark = pytest.mark.skipif(not HAS_SPARK, reason="pyspark and Java not installed")
needs_ray = pytest.mark.skipif(not HAS_RAY, reason="ray not installed")
needs_spark_ray = pytest.mark.skipif(not (HAS_SPARK and HAS_RAY), reason="Spark + Ray not installed")


@pytest.fixture(scope="module")
def orch():
    """ssh-executor's ``training`` module (see test_weighted_shards for why)."""
    from sqlalchemy.exc import InvalidRequestError

    from tests.conftest import SERVICES_DIR, import_service_module

    svc = str(SERVICES_DIR / "ssh-executor")
    loaded = sys.modules.get("models")
    if loaded is not None and str(getattr(loaded, "__file__", "")).startswith(svc + os.sep):
        sys.path.insert(0, svc)
        try:
            return importlib.import_module("training")
        finally:
            sys.path.remove(svc)
    try:
        return import_service_module("ssh-executor", "training")
    except InvalidRequestError:
        from shared.common.database import Base

        for table in list(Base.metadata.tables.values()):
            if table.name in {"ssh_nodes", "pending_provisions", "resource_leases",
                              "task_batches", "tasks", "training_runs"}:
                Base.metadata.remove(table)
        return import_service_module("ssh-executor", "training")


@pytest.fixture(scope="module")
def ml(orch):
    """The ml package, loaded through training so module identities match."""
    return types.SimpleNamespace(
        params=importlib.import_module("ml.params"),
        custom=importlib.import_module("ml.custom_model"),
        engines=importlib.import_module("ml.engines"),
    )


@pytest.fixture(scope="module", autouse=True)
def _stop_engines_after(orch):
    yield
    importlib.import_module("ml.engines").shutdown_engines()


# --- average_params -------------------------------------------------------
def test_average_params_weights_by_rows(ml):
    out = ml.params.average_params([({"w": [0.0, 2.0], "b": 0.0}, 1), ({"w": [4.0, 6.0], "b": 8.0}, 3)])
    assert out["w"] == pytest.approx([3.0, 5.0])
    assert out["b"] == pytest.approx(6.0)


def test_average_params_nested_and_identical_values_kept(ml):
    a = {"layers": [[1.0, 2.0], [3.0, 4.0]], "arch": {"F": 8, "side": 4}, "name": "net"}
    b = {"layers": [[3.0, 4.0], [5.0, 6.0]], "arch": {"F": 8, "side": 4}, "name": "net"}
    out = ml.params.average_params([(a, 1), (b, 1)])
    assert out["layers"] == [[2.0, 3.0], [4.0, 5.0]]
    assert out["arch"] == {"F": 8, "side": 4} and isinstance(out["arch"]["F"], int)
    assert out["name"] == "net"


def test_average_params_skips_empty_and_rejects_mismatches(ml):
    assert ml.params.average_params([({"w": [1.0]}, 0)]) is None
    assert ml.params.average_params([(None, 5), ({"w": [2.0]}, 4)]) == {"w": [2.0]}
    with pytest.raises(ValueError, match="different keys"):
        ml.params.average_params([({"a": 1.0}, 1), ({"b": 1.0}, 1)])
    with pytest.raises(ValueError, match="different lengths"):
        ml.params.average_params([({"w": [1.0]}, 1), ({"w": [1.0, 2.0]}, 1)])
    with pytest.raises(ValueError, match="non-numeric"):
        ml.params.average_params([({"act": "relu"}, 1), ({"act": "tanh"}, 1)])


def test_combine_metrics_is_row_weighted(ml):
    out = ml.params.combine_metrics([({"mse": 1.0, "tag": "x"}, 1), ({"mse": 4.0}, 3), (None, 5)])
    assert out == {"mse": pytest.approx(3.25), "n": 4}


def test_builtin_aggregate_matches_old_fedavg(orch):
    agg = orch.aggregate([{"w": [0.0], "b": 0.0, "n": 1, "loss": 9.0}, {"w": [4.0], "b": 8.0, "n": 3}])
    assert agg == {"w": [pytest.approx(3.0)], "b": pytest.approx(6.0)}


# --- custom model runner (executed exactly like a worker task) -------------
def _run_task(tmp_path, code, files, stdin, name="t"):
    d = tmp_path / f"{name}-{uuid.uuid4().hex[:6]}"
    d.mkdir()
    (d / "task.py").write_text(code)
    for fname, content in files.items():
        (d / fname).write_text(content)
    return subprocess.run(
        [sys.executable, "-I", "-B", "task.py"], cwd=d, input=stdin,
        capture_output=True, text=True, timeout=60,
    )


def test_runner_check_init_train_evaluate(ml, tmp_path):
    cm = ml.custom
    model = cm.STARTER_TEMPLATE.replace("def train(", "print('noise from user code')\n\n\ndef train(", 1)
    files = cm.files(model)
    rows = [[float(x), 2.0 * x + 1.0] for x in range(1, 10)]
    cfg = {"feature_names": ["x"], "learning_rate": 0.01, "epochs": 50}

    check = cm.parse_output(_run_task(tmp_path, cm.RUNNER_SOURCE, files, cm.payload("check")).stdout)
    assert check["ok"] and check["has_init"] and check["has_evaluate"]

    init = cm.parse_output(_run_task(tmp_path, cm.RUNNER_SOURCE, files, cm.payload("init", config=cfg)).stdout)
    assert init["params"] == {"w": [0.0], "b": 0.0}

    proc = _run_task(tmp_path, cm.RUNNER_SOURCE, files,
                     cm.payload("train", params=init["params"], rows=rows, config=cfg))
    assert proc.returncode == 0, proc.stderr
    out = cm.parse_output(proc.stdout)
    assert out["n"] == 9 and out["metrics"]["mse"] > 0
    assert 1.5 < out["params"]["w"][0] < 2.5


def test_runner_converts_arrays_and_rejects_nan(ml, tmp_path):
    cm = ml.custom
    arrays = (
        "class Arr(list):\n    def tolist(self):\n        return list(self)\n\n"
        "def train(params, rows, config):\n    return {'w': Arr([1.0, 2.0])}\n"
    )
    out = cm.parse_output(_run_task(tmp_path, cm.RUNNER_SOURCE, cm.files(arrays), cm.payload("train", rows=[[1.0, 2.0]])).stdout)
    assert out["params"] == {"w": [1.0, 2.0]}

    nan = "def train(params, rows, config):\n    return {'w': [float('nan')]}\n"
    proc = _run_task(tmp_path, cm.RUNNER_SOURCE, cm.files(nan), cm.payload("train", rows=[[1.0, 2.0]]))
    assert proc.returncode != 0 and cm.parse_output(proc.stdout) is None


def test_validate_model_code(ml):
    cm = ml.custom
    cm.validate_model_code(cm.STARTER_TEMPLATE)
    with pytest.raises(ValueError, match="syntax error on line 1"):
        cm.validate_model_code("def train(:\n")
    with pytest.raises(ValueError, match="train"):
        cm.validate_model_code("def fit(params, rows, config):\n    return params\n")
    with pytest.raises(ValueError, match="empty"):
        cm.validate_model_code("   ")


# --- engine detection -----------------------------------------------------
def test_engine_detection_reports_missing_parts(ml, monkeypatch):
    eng = ml.engines
    monkeypatch.setattr(eng, "_package_version", lambda name: None if name == "ray" else "3.5.1")
    monkeypatch.setattr(eng, "_java_executable", lambda: None)
    items = {e["id"]: e for e in eng.list_engines(refresh=True)}
    assert items["python"]["available"] is True
    spark_ray = items["spark_ray"]
    assert spark_ray["available"] is False
    assert spark_ray["missing"] == ["java", "ray"]
    assert "Enable Spark + Ray" in spark_ray["hint"]
    with pytest.raises(eng.EngineUnavailable):
        eng.require_engine("spark_ray")
    eng.list_engines(refresh=True)  # restore the real view for later tests


# --- the training API rejects requests that cannot run --------------------
@pytest.fixture()
def api(orch, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shared.common.auth import get_current_user

    routes = sys.modules["routes"] if "routes" in sys.modules and hasattr(sys.modules["routes"], "train_router") else None
    if routes is None:
        from tests.conftest import SERVICES_DIR

        sys.path.insert(0, str(SERVICES_DIR / "ssh-executor"))
        try:
            routes = importlib.import_module("routes")
        finally:
            sys.path.remove(str(SERVICES_DIR / "ssh-executor"))

    class _Session:
        def add(self, obj):
            obj.id = uuid.uuid4()
            obj.status = obj.status or "pending"
            import datetime as _dt
            obj.created_at = _dt.datetime.now(_dt.timezone.utc)

        async def commit(self):
            pass

        async def refresh(self, obj):
            pass

    async def _db():
        yield _Session()

    started = []
    monkeypatch.setattr(routes, "run_training", lambda run_id: started.append(run_id))
    app = FastAPI()
    app.include_router(routes.train_router, prefix="/api/v1/ssh")
    app.dependency_overrides[get_current_user] = lambda: {"sub": "dev-user"}
    app.dependency_overrides[routes.get_db_session] = _db
    return types.SimpleNamespace(client=TestClient(app), routes=routes, started=started)


DS = str(uuid.uuid4())


@pytest.mark.parametrize("body, needle", [
    ({"model_type": "spark:RandomForestClassifier", "target_column": "y"}, "spark_ray"),
    ({"model_type": "custom", "mode": "single", "target_column": "y", "model_code": "def train(p, r, c):\n    return p\n"}, "multi"),
    ({"model_type": "custom", "target_column": "y", "model_code": "def train(:\n"}, "syntax error"),
    ({"model_type": "custom", "target_column": "y"}, "empty"),
    ({"model_type": "linear_regression", "target_column": "y", "model_params": {"a": 1}}, "built-in"),
    ({"model_type": "linear_regression", "target_column": "y", "model_code": "x"}, "custom"),
    ({"model_type": "linear_regression"}, "target_column"),
])
def test_train_request_validation(api, body, needle):
    r = api.client.post("/api/v1/ssh/train", json={"dataset_id": DS, **body})
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]
    assert not api.started


def test_spark_ray_request_rejected_when_not_installed(api, monkeypatch):
    def unavailable(engine):
        raise api.routes.EngineUnavailable("Spark + Ray is not installed on the master.")

    monkeypatch.setattr(api.routes, "require_engine", unavailable)
    r = api.client.post("/api/v1/ssh/train", json={
        "dataset_id": DS, "model_type": "linear_regression", "target_column": "y", "engine": "spark_ray",
    })
    assert r.status_code == 400 and "not installed" in r.json()["detail"]


def test_valid_custom_request_is_stored_with_code(api):
    code = "def train(params, rows, config):\n    return params\n"
    r = api.client.post("/api/v1/ssh/train", json={
        "dataset_id": DS, "model_type": "custom", "target_column": "y", "model_code": code,
        "model_params": {"hidden": 8},
    })
    assert r.status_code == 201, r.text
    assert r.json()["model_family"] == "custom" and r.json()["model_format"] == "json"
    assert len(api.started) == 1


def test_custom_template_endpoint(api, ml):
    r = api.client.get("/api/v1/ssh/models/custom/template")
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "model.py"
    ml.custom.validate_model_code(body["code"])


def test_engines_endpoint(api):
    r = api.client.get("/api/v1/ssh/engines")
    assert r.status_code == 200
    ids = [e["id"] for e in r.json()["items"]]
    assert ids == ["python", "spark_ray"]


# --- full runs with fakes for the database, leases and SSH ----------------
def _fake_run(model_type, mode, hyper, model_code=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(), owner_id="dev-user", dataset_id=uuid.uuid4(), model_type=model_type,
        mode=mode, rounds=hyper.pop("rounds", 2), model_code=model_code, status="pending",
        hyperparams_json=json.dumps({"learning_rate": 0.01, "epochs": 20, "local_cores": 2, **hyper}),
    )


def _patch_db(orch, monkeypatch, run):
    class _Result:
        def scalar_one_or_none(self):
            return run

    class _Session:
        async def execute(self, _stmt):
            return _Result()

    class _DB:
        @contextlib.asynccontextmanager
        async def session(self):
            yield _Session()

    async def update(run_id, **fields):
        for key, value in fields.items():
            setattr(run, key, value)

    monkeypatch.setattr(orch, "get_db_manager", lambda: _DB())
    monkeypatch.setattr(orch, "_update_run", update)


def _patch_workers(orch, monkeypatch, tmp_path, n_nodes=2):
    nodes = [types.SimpleNamespace(id=f"node-{i}", name=f"worker-{i}", allocated_cpu=1,
                                   allocated_gpu_memory_gb=0.0) for i in range(n_nodes)]

    class _Alloc:
        def __init__(self, node):
            self.node, self.cpu_allocated, self.slots, self.gpu_allocated = node, 1, 1, 0

    async def online(owner_id, node_ids=None):
        return nodes

    async def reserve(owner_id, nodes_, **kw):
        return [_Alloc(n) for n in nodes_]

    async def release(owner_id, allocations):
        pass

    @contextlib.asynccontextmanager
    async def heartbeat(*a, **kw):
        yield

    calls = []

    async def fan_out(allocations, code, inputs, *, files_for=None, affinity=None, **kw):
        out = []
        for i, stdin in enumerate(inputs):
            proc = await asyncio.to_thread(
                _run_task, tmp_path, code, files_for(i) if files_for else {}, stdin
            )
            node = allocations[i % len(allocations)].node
            out.append({"status": "completed" if proc.returncode == 0 else "failed",
                        "exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
                        "node_id": node.id, "attempts": 1})
        calls.append(len(inputs))
        return out

    monkeypatch.setattr(orch, "_online_nodes", online)
    monkeypatch.setattr(orch, "reserve_allocations", reserve)
    monkeypatch.setattr(orch, "release_allocations", release)
    monkeypatch.setattr(orch, "lease_heartbeat", heartbeat)
    monkeypatch.setattr(orch, "run_across_nodes", fan_out)
    return calls


def _linear_rows(n=60, seed=0):
    rng = random.Random(seed)
    return [[x, 2.0 * x + 1.0 + rng.uniform(-0.05, 0.05)] for x in (rng.uniform(0, 1) for _ in range(n))]


async def test_custom_model_run_on_workers(orch, ml, monkeypatch, tmp_path):
    run = _fake_run("custom", "multi", {"target_column": "y", "feature_columns": ["x"], "rounds": 3, "epochs": 30},
                    model_code=ml.custom.STARTER_TEMPLATE)
    _patch_db(orch, monkeypatch, run)
    calls = _patch_workers(orch, monkeypatch, tmp_path)

    async def load(*a, **kw):
        return _linear_rows(), ["x"]

    monkeypatch.setattr(orch, "_load_dataset_rows", load)
    await orch.run_training(run.id)

    assert run.status == "completed", run.message
    # check (1 per node) + init + 3 rounds + final evaluate.
    assert calls == [2, 1, 2, 2, 2, 2]
    history = json.loads(run.history_json)
    assert [h["round"] for h in history] == [1, 2, 3]
    assert history[-1]["metrics"]["mse"] < history[0]["metrics"]["mse"]
    params = json.loads(run.weights_json)["params"]
    assert 1.0 < params["w"][0] < 3.0
    assert json.loads(run.details_json)["custom"] == {"has_init": True, "has_evaluate": True}


async def test_custom_model_check_failure_names_the_node(orch, ml, monkeypatch, tmp_path):
    code = "import not_installed_pkg\n\ndef train(params, rows, config):\n    return params\n"
    run = _fake_run("custom", "multi", {"target_column": "y"}, model_code=code)
    _patch_db(orch, monkeypatch, run)
    _patch_workers(orch, monkeypatch, tmp_path, n_nodes=1)

    async def load(*a, **kw):
        return _linear_rows(10), ["x"]

    monkeypatch.setattr(orch, "_load_dataset_rows", load)
    await orch.run_training(run.id)
    assert run.status == "failed"
    assert "worker-0" in run.message and "not_installed_pkg" in run.message


# --- real Spark -----------------------------------------------------------
def _write_csv(path, header, rows):
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    return str(path)


def _classification_rows(n=400, seed=1):
    rng = random.Random(seed)
    out = []
    for _ in range(n):
        a, b = rng.uniform(0, 1), rng.uniform(0, 1)
        out.append([round(a, 4), round(b, 4), "yes" if a + b > 1 else "no"])
    return out


@needs_spark
def test_spark_reads_csv_json_parquet_and_samples(orch, tmp_path):
    sp = importlib.import_module("ml.spark_pipeline")
    path = _write_csv(tmp_path / "d.csv", ["x", "label", "y"],
                      [[i, "a", 2 * i + 1] for i in range(50)] + [["", "a", 3]])
    df = sp.read_dataframe(path, "csv")
    rows, features, details = sp.dataframe_rows(df, "y", [], max_rows=20, seed=7)
    assert features == ["x"]                       # the text column is skipped
    assert details["rows_total"] == 50 and details["sampled"] and len(rows) == 20
    assert all(r[1] == 2 * r[0] + 1 for r in rows)

    df.write.parquet(str(tmp_path / "p"))
    pq = sp.read_dataframe(str(tmp_path / "p"), "parquet")
    assert pq.count() == 51

    jpath = tmp_path / "d.jsonl"
    jpath.write_text("\n".join(json.dumps({"x": i, "y": i * 3}) for i in range(5)))
    assert sp.read_dataframe(str(jpath), "jsonl").count() == 5
    apath = tmp_path / "a.json"
    apath.write_text(json.dumps([{"x": 1, "y": 2}, {"x": 2, "y": 4}]))
    assert sp.read_dataframe(str(apath), "json").count() == 2


@needs_spark
def test_spark_catalog_has_every_supported_estimator(orch):
    sml = importlib.import_module("ml.spark_ml")
    cat = sml.catalog()
    for name in ("spark:RandomForestClassifier", "spark:GBTRegressor", "spark:KMeans",
                 "spark:LogisticRegression", "spark:LinearRegression", "spark:NaiveBayes"):
        assert name in cat
    assert "spark:OneVsRest" not in cat and "spark:LDA" not in cat
    rf = {p["name"]: p for p in cat["spark:RandomForestClassifier"]["params"]}
    assert rf["numTrees"]["default"] == 20 and rf["numTrees"]["type"] == "int"
    mlp = {p["name"]: p for p in cat["spark:MultilayerPerceptronClassifier"]["params"]}
    assert mlp["layers"]["required"] is True
    with pytest.raises(ValueError, match="layers"):
        sml.validate_params("spark:MultilayerPerceptronClassifier", {}, {})
    with pytest.raises(ValueError, match="not a parameter"):
        sml.validate_params("spark:KMeans", {"numTrees": 3}, {})
    with pytest.raises(ValueError, match="not supported here"):
        sml.model_info("spark:OneVsRest")


@needs_spark
def test_spark_random_forest_with_grid_search(orch, tmp_path):
    from pyspark.ml import PipelineModel

    sp = importlib.import_module("ml.spark_pipeline")
    sml = importlib.import_module("ml.spark_ml")
    path = _write_csv(tmp_path / "c.csv", ["a", "b", "label"], _classification_rows())
    df = sp.read_dataframe(path, "csv")
    metrics, details, artifact = sml.train_spark_model(
        df, "spark:RandomForestClassifier", target="label", feature_columns=[],
        params={"seed": 3}, grid={"numTrees": [5, 20]}, test_fraction=0.25, max_rows=10_000,
    )
    assert metrics["accuracy"] > 0.85 and "areaUnderROC" in metrics
    assert details["labels"] and set(details["labels"]) == {"yes", "no"}
    assert set(details["feature_importances"]) == {"a", "b"}
    assert len(details["grid_search"]["candidates"]) == 2

    out = tmp_path / "model"
    with zipfile.ZipFile(__import__("io").BytesIO(artifact)) as zf:
        zf.extractall(out)
    reloaded = PipelineModel.load(str(out))
    # Predicting needs only the feature columns, and returns the original labels.
    scored = reloaded.transform(df.select("a", "b"))
    assert scored.count() == 400
    assert {r.predicted_label for r in scored.select("predicted_label").collect()} <= {"yes", "no"}


@needs_spark
def test_spark_regression_and_clustering(orch, tmp_path):
    sp = importlib.import_module("ml.spark_pipeline")
    sml = importlib.import_module("ml.spark_ml")
    reg = sp.read_dataframe(_write_csv(tmp_path / "r.csv", ["x", "y"], _linear_rows(200)), "csv")
    metrics, details, _ = sml.train_spark_model(
        reg, "spark:LinearRegression", target="y", feature_columns=["x"], params={}, grid={},
        test_fraction=0.2, max_rows=10_000,
    )
    assert metrics["r2"] > 0.99 and details["coefficients"]["x"] == pytest.approx(2.0, abs=0.05)

    pts = [[0.0 + random.random() * 0.1, 0.0] for _ in range(30)] + [[5.0 + random.random() * 0.1, 5.0] for _ in range(30)]
    clu = sp.read_dataframe(_write_csv(tmp_path / "k.csv", ["p", "q"], pts), "csv")
    metrics, details, _ = sml.train_spark_model(
        clu, "spark:KMeans", target="", feature_columns=[], params={"k": 2, "seed": 1}, grid={},
        test_fraction=0.2, max_rows=10_000,
    )
    assert metrics["silhouette"] > 0.9 and details["cluster_count"] == 2


# --- real Ray -------------------------------------------------------------
@needs_ray
def test_ray_parameter_server_matches_python_average(orch, ml):
    ro = importlib.import_module("ml.ray_orchestrator")
    pairs = [({"w": [0.0, 2.0], "b": 0.0, "arch": {"F": 8}}, 1), ({"w": [4.0, 6.0], "b": 8.0, "arch": {"F": 8}}, 3)]
    server = ro.ParameterServer({"w": [0.0, 0.0], "b": 0.0})
    try:
        assert server.apply(pairs) == ml.params.average_params(pairs)
        assert server.apply([(None, 0)]) is None
        stats = server.stats()
        assert stats["version"] == 1 and stats["updates"][0] == {"version": 1, "shards": 2, "rows": 4}
    finally:
        server.close()


@needs_ray
def test_ray_trains_shards_like_the_workers(orch):
    ro = importlib.import_module("ml.ray_orchestrator")
    shards = [_linear_rows(20, seed=s) for s in range(3)]
    got = ro.train_shards_parallel(orch.builtin_helpers_source(), "linear_regression", 0.05, 10, None, shards)
    want = [orch.train_core("linear_regression", 0.05, 10, None, s) for s in shards]
    for g, w in zip(got, want):
        assert g["n"] == w["n"] and g["w"] == pytest.approx(w["w"]) and g["b"] == pytest.approx(w["b"])


def _patch_spark_dataset(orch, monkeypatch, tmp_path, header, rows):
    source = _write_csv(tmp_path / "source.csv", header, rows)

    async def metadata(dataset_id, settings, owner_id):
        return {"id": str(dataset_id), "file_format": "csv"}

    def download(meta, settings, owner_id):
        copy = tmp_path / f"dl-{uuid.uuid4().hex[:6]}.csv"
        shutil.copy(source, copy)
        return str(copy)

    monkeypatch.setattr(orch, "_fetch_metadata", metadata)
    monkeypatch.setattr(orch, "_download_to_file", download)


@needs_spark_ray
async def test_spark_ray_engine_single_machine_run(orch, monkeypatch, tmp_path):
    run = _fake_run("linear_regression", "single",
                    {"engine": "spark_ray", "target_column": "y", "feature_columns": [], "rounds": 3, "epochs": 30})
    _patch_db(orch, monkeypatch, run)
    _patch_spark_dataset(orch, monkeypatch, tmp_path, ["x", "y"], _linear_rows(80))
    monkeypatch.setattr(orch, "_online_nodes", lambda *a: (_ for _ in ()).throw(AssertionError("used SSH")))
    await orch.run_training(run.id)

    assert run.status == "completed", run.message
    details = json.loads(run.details_json)
    assert details["engine"] == "spark_ray"
    assert details["data"]["loader"] == "spark" and details["data"]["rows_used"] == 80
    assert details["ray"]["version"] == 3                  # one parameter-server update per round
    assert all(s["node_id"] == "ray" for s in json.loads(run.shards_json))
    assert json.loads(run.metrics_json)["r2"] > 0.5


@needs_spark_ray
async def test_spark_ray_engine_custom_model_on_workers(orch, ml, monkeypatch, tmp_path):
    run = _fake_run("custom", "multi", {"engine": "spark_ray", "target_column": "y", "rounds": 2, "epochs": 30},
                    model_code=ml.custom.STARTER_TEMPLATE)
    _patch_db(orch, monkeypatch, run)
    _patch_workers(orch, monkeypatch, tmp_path)
    _patch_spark_dataset(orch, monkeypatch, tmp_path, ["x", "y"], _linear_rows(40))
    await orch.run_training(run.id)

    assert run.status == "completed", run.message
    details = json.loads(run.details_json)
    assert details["ray"]["version"] == 2 and details["data"]["loader"] == "spark"


@needs_spark_ray
async def test_spark_mllib_run_stores_artifact(orch, monkeypatch, tmp_path):
    run = _fake_run("spark:GBTClassifier", "multi",
                    {"engine": "spark_ray", "target_column": "label", "model_params": {"maxIter": 10}})
    _patch_db(orch, monkeypatch, run)
    _patch_spark_dataset(orch, monkeypatch, tmp_path, ["a", "b", "label"], _classification_rows())
    monkeypatch.setattr(orch, "_online_nodes", lambda *a: (_ for _ in ()).throw(AssertionError("used SSH")))
    await orch.run_training(run.id)

    assert run.status == "completed", run.message
    assert json.loads(run.metrics_json)["accuracy"] > 0.85
    assert run.artifact[:2] == b"PK"                       # zipped PipelineModel
    assert json.loads(run.details_json)["spark"]["model"] == "GBTClassifier"
    assert "GBTClassifier" in run.message


@needs_spark_ray
async def test_spark_mllib_bad_data_fails_with_readable_message(orch, monkeypatch, tmp_path):
    run = _fake_run("spark:LinearRegression", "multi", {"engine": "spark_ray", "target_column": "nope"})
    _patch_db(orch, monkeypatch, run)
    _patch_spark_dataset(orch, monkeypatch, tmp_path, ["x", "y"], _linear_rows(10))
    await orch.run_training(run.id)
    assert run.status == "failed"
    assert run.message == "Spark training failed: Target column 'nope' not found in dataset"


def test_storage_errors_keep_the_storage_message(orch):
    """A run on an unreadable dataset says why (e.g. HDFS is off), not just "503"."""
    import httpx

    request = httpx.Request("GET", "http://storage-service:8001/api/v1/storage/datasets/x/download")
    stored_in_hdfs = httpx.Response(503, request=request, json={
        "error_code": "STORAGE_UNAVAILABLE",
        "message": "This dataset is stored in HDFS, which is not running.",
    })
    with pytest.raises(RuntimeError, match="Could not read the dataset: This dataset is stored in HDFS"):
        orch._check_storage_response(stored_in_hdfs)

    with pytest.raises(httpx.HTTPStatusError):
        orch._check_storage_response(httpx.Response(502, request=request, text="bad gateway"))
    orch._check_storage_response(httpx.Response(200, request=request, content=b"a,b\n"))

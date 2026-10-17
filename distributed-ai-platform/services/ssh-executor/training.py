"""training — Unified distributed ML training orchestrator, model templates, and ONNX export.

Consolidated module combining:
- Federated averaging math & remote training script generation (formerly ml_templates.py)
- Model artifact export to portable ONNX graphs (formerly onnx_export.py)
- Multi-node training orchestrator & compute-weighted sharding (formerly training_orchestrator.py)
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
import csv
from dataclasses import dataclass, field
import inspect
import io
import json
import math
import os
import tempfile
from typing import Optional
import uuid

import httpx
import onnx
from onnx import TensorProto, helper
from sqlalchemy import select
import structlog

from config import SSHExecutorSettings, get_settings
from database import get_db_manager
from tasks import lease_heartbeat, run_across_nodes
from leases import (
    node_gpu_enforceable,
    node_gpu_fraction,
    release_allocations,
    reserve_allocations,
)
from ml import custom_model
from ml.engines import PYTHON, SPARK_RAY, EngineUnavailable, require_engine
from ml.params import average_params, combine_metrics
from models import SSHNode, TrainingRun
from runtimes import policy_nice
from shared.common.auth import create_internal_token

logger = structlog.get_logger(__name__)

# ==============================================================================
# 1. CORE ML MATH & LOCAL/REMOTE TRAINING LOGIC (from ml_templates)
# ==============================================================================

def _predict_linear(w, b, x):
    return sum(wi * xi for wi, xi in zip(w, x)) + b


def _predict_logistic(w, b, x):
    z = sum(wi * xi for wi, xi in zip(w, x)) + b
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def train_core(model_type, lr, epochs, weights, rows):
    """Run epochs passes of SGD over rows starting from weights."""
    if not rows:
        base = weights or {}
        return {"w": list(base.get("w", [])), "b": float(base.get("b", 0.0)), "n": 0, "loss": 0.0}
    d = len(rows[0]) - 1
    if weights and weights.get("w"):
        w = list(weights["w"])
        b = float(weights.get("b", 0.0))
    else:
        w = [0.0] * d
        b = 0.0
    n = len(rows)
    for _ in range(int(epochs)):
        for row in rows:
            x = row[:d]
            y = float(row[d])
            if model_type == "logistic_regression":
                pred = _predict_logistic(w, b, x)
            else:
                pred = _predict_linear(w, b, x)
            err = pred - y
            for j in range(d):
                w[j] -= lr * err * x[j]
            b -= lr * err
    loss = 0.0
    for row in rows:
        x = row[:d]
        y = float(row[d])
        if model_type == "logistic_regression":
            p = _predict_logistic(w, b, x)
            eps = 1e-12
            loss += -(y * math.log(p + eps) + (1 - y) * math.log(1 - p + eps))
        else:
            p = _predict_linear(w, b, x)
            loss += (p - y) ** 2
    loss /= n
    return {"w": w, "b": b, "n": n, "loss": loss}


def _cnn_init(arch: dict, seed: int = 1234) -> list:
    import random

    f = int(arch["F"]); side = int(arch["side"]); c = int(arch["C"])
    ph = (side - 2) // 2
    flat = f * ph * ph
    rng = random.Random(seed)
    s1 = (2.0 / 9.0) ** 0.5
    s2 = (2.0 / max(1, flat)) ** 0.5
    out = [rng.uniform(-s1, s1) for _ in range(f * 9)] + [0.0] * f
    out += [rng.uniform(-s2, s2) for _ in range(flat * c)] + [0.0] * c
    return out


def _evaluate_cnn(weights: dict, rows: list) -> dict:
    params = weights.get("w") or []
    arch = weights.get("arch") or {}
    if not params or not rows:
        return {}
    d = len(rows[0]) - 1
    f = int(arch.get("F", 8))
    side = int(arch.get("side", 0)) or int(d ** 0.5)
    side -= side % 2
    if side < 4:
        side = 4
    c = int(arch.get("C", 0)) or (max(int(round(float(r[d]))) for r in rows) + 1)
    px = side * side
    oh = side - 2
    ph = oh // 2
    flat = f * ph * ph
    fc_base = f * 9 + f
    fc_w0 = fc_base
    fc_b0 = fc_base + flat * c

    def predict(row) -> int:
        x = [float(v) for v in row[:px]] + [0.0] * max(0, px - len(row[:d]))
        pooled = [0.0] * flat
        for ff in range(f):
            cb = params[f * 9 + ff]
            base_w = ff * 9
            for a in range(ph):
                for b in range(ph):
                    best = None
                    for di in range(2):
                        i = a * 2 + di
                        for dj in range(2):
                            j = b * 2 + dj
                            s = cb
                            for u in range(3):
                                for v in range(3):
                                    s += params[base_w + u * 3 + v] * x[(i + u) * side + (j + v)]
                            if s < 0.0:
                                s = 0.0
                            if best is None or s > best:
                                best = s
                    pooled[ff * ph * ph + a * ph + b] = best if best is not None else 0.0
        best_c, best_logit = 0, None
        for cc in range(c):
            s = params[fc_b0 + cc]
            for k in range(flat):
                s += params[fc_w0 + k * c + cc] * pooled[k]
            if best_logit is None or s > best_logit:
                best_logit, best_c = s, cc
        return best_c

    n = len(rows)
    correct = 0
    tp = [0] * c; fp = [0] * c; fn = [0] * c
    for row in rows:
        y = int(round(float(row[d])))
        y = 0 if y < 0 else (c - 1 if y >= c else y)
        p = predict(row)
        if p == y:
            correct += 1
            tp[y] += 1
        else:
            fp[p] += 1
            fn[y] += 1
    f1s = []
    for cc in range(c):
        prec = tp[cc] / (tp[cc] + fp[cc]) if (tp[cc] + fp[cc]) else 0.0
        rec = tp[cc] / (tp[cc] + fn[cc]) if (tp[cc] + fn[cc]) else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if (prec + rec) else 0.0)
    return {
        "accuracy": round(correct / n if n else 0.0, 4),
        "f1": round(sum(f1s) / len(f1s) if f1s else 0.0, 4),
        "classes": c,
        "n": n,
    }


def _train_cnn(weights: dict, rows: list, lr: float, epochs: int) -> dict:
    arch = (weights or {}).get("arch") or {}
    params = list((weights or {}).get("w") or [])
    if not rows or not params:
        return {"w": params, "b": 0.0, "arch": arch, "n": 0, "loss": 0.0}
    d = len(rows[0]) - 1
    f = int(arch.get("F", 8))
    side = int(arch.get("side", 0)) or int(d ** 0.5)
    side -= side % 2
    if side < 4:
        side = 4
    c = int(arch.get("C", 0)) or (max(int(round(float(r[d]))) for r in rows) + 1)
    px = side * side
    oh = side - 2
    ph = oh // 2
    flat = f * ph * ph
    conv_b0 = f * 9
    fc_w0 = f * 9 + f
    fc_b0 = fc_w0 + flat * c
    n = len(rows)
    total_loss = 0.0
    reps = max(1, int(epochs))
    for _ in range(reps):
        for row in rows:
            x = [float(v) for v in row[:px]]
            if len(x) < px:
                x += [0.0] * (px - len(x))
            y = int(round(float(row[d])))
            y = 0 if y < 0 else (c - 1 if y >= c else y)
            conv_act = [[0.0] * (oh * oh) for _ in range(f)]
            for ff in range(f):
                cb = params[conv_b0 + ff]
                bw = ff * 9
                for i in range(oh):
                    for j in range(oh):
                        s = cb
                        for u in range(3):
                            base = (i + u) * side + j
                            w3 = bw + u * 3
                            s += (params[w3] * x[base]
                                  + params[w3 + 1] * x[base + 1]
                                  + params[w3 + 2] * x[base + 2])
                        conv_act[ff][i * oh + j] = s if s > 0.0 else 0.0
            pooled = [0.0] * flat
            arg = [0] * flat
            for ff in range(f):
                for a in range(ph):
                    for b2 in range(ph):
                        best = None
                        bidx = 0
                        for di in range(2):
                            ii = a * 2 + di
                            for dj in range(2):
                                jj = b2 * 2 + dj
                                val = conv_act[ff][ii * oh + jj]
                                if best is None or val > best:
                                    best = val
                                    bidx = ii * oh + jj
                        k = ff * ph * ph + a * ph + b2
                        pooled[k] = best if best is not None else 0.0
                        arg[k] = bidx
            logits = [0.0] * c
            for cc in range(c):
                s = params[fc_b0 + cc]
                for k in range(flat):
                    s += params[fc_w0 + k * c + cc] * pooled[k]
                logits[cc] = s
            m = max(logits)
            exps = [math.exp(v - m) for v in logits]
            ssum = sum(exps) or 1.0
            probs = [e / ssum for e in exps]
            total_loss += -math.log(max(probs[y], 1e-12))
            dlogits = [probs[cc] - (1.0 if cc == y else 0.0) for cc in range(c)]
            dpooled = [0.0] * flat
            for k in range(flat):
                pk = pooled[k]
                base = fc_w0 + k * c
                dp = 0.0
                for cc in range(c):
                    g = dlogits[cc]
                    dp += params[base + cc] * g
                    params[base + cc] -= lr * g * pk
                dpooled[k] = dp
            for cc in range(c):
                params[fc_b0 + cc] -= lr * dlogits[cc]
            for ff in range(f):
                bw = ff * 9
                for a in range(ph):
                    for b2 in range(ph):
                        k = ff * ph * ph + a * ph + b2
                        g = dpooled[k]
                        if g == 0.0 or pooled[k] <= 0.0:
                            continue
                        cell = arg[k]
                        i = cell // oh
                        j = cell % oh
                        for u in range(3):
                            base = (i + u) * side + j
                            w3 = bw + u * 3
                            params[w3] -= lr * g * x[base]
                            params[w3 + 1] -= lr * g * x[base + 1]
                            params[w3 + 2] -= lr * g * x[base + 2]
                        params[conv_b0 + ff] -= lr * g
    return {"w": params, "b": 0.0, "arch": arch, "n": n, "loss": total_loss / (n * reps)}


def evaluate(model_type: str, weights: Optional[dict], rows: list) -> dict:
    if not weights or not weights.get("w") or not rows:
        return {}
    if model_type == "cnn":
        return _evaluate_cnn(weights, rows)
    w = weights["w"]
    b = float(weights.get("b", 0.0))
    d = len(w)
    if model_type == "logistic_regression":
        tp = tn = fp = fn = correct = n = 0
        for row in rows:
            x = row[:d]
            y = int(round(float(row[d])))
            pred = 1 if _predict_logistic(w, b, x) >= 0.5 else 0
            n += 1
            if pred == y:
                correct += 1
            if pred == 1 and y == 1:
                tp += 1
            elif pred == 0 and y == 0:
                tn += 1
            elif pred == 1 and y == 0:
                fp += 1
            else:
                fn += 1
        acc = correct / n if n else 0.0
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        return {
            "accuracy": round(acc, 4),
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(f1, 4),
            "n": n,
        }
    ys = []
    sse = 0.0
    for row in rows:
        x = row[:d]
        y = float(row[d])
        sse += (_predict_linear(w, b, x) - y) ** 2
        ys.append(y)
    n = len(ys)
    mean = sum(ys) / n if n else 0.0
    sst = sum((y - mean) ** 2 for y in ys)
    mse = sse / n if n else 0.0
    r2 = 1 - sse / sst if sst > 0 else 0.0
    return {"mse": round(mse, 6), "rmse": round(mse ** 0.5, 6), "r2": round(r2, 4), "n": n}


SUPPORTED_MODELS = ("linear_regression", "logistic_regression", "cnn")
REMOTE_SCRIPT_USES_GPU = False


def builtin_helpers_source() -> str:
    """Self-contained stdlib source of the built-in trainers (workers and Ray run it)."""
    return "import math\n\n" + "\n\n".join(
        inspect.getsource(fn)
        for fn in (_predict_linear, _predict_logistic, train_core, _train_cnn)
    )


def generate_remote_script(model_type: str, lr: float, epochs: int) -> str:
    if model_type not in SUPPORTED_MODELS:
        raise ValueError(f"Unsupported model_type: {model_type}")
    helpers = builtin_helpers_source()
    if model_type == "cnn":
        train_call = "    out = _train_cnn(weights, rows, %r, %r)\n" % (float(lr), int(epochs))
    else:
        train_call = (
            "    out = train_core(%r, %r, %r, weights, rows)\n"
            % (model_type, float(lr), int(epochs))
        )
    return (
        "import sys, json, math\n\n"
        + f"{helpers}\n\n"
        + "def _main():\n"
        + '    payload = json.loads(sys.stdin.read() or "{}")\n'
        + '    rows = payload.get("rows", [])\n'
        + '    weights = payload.get("weights")\n'
        + train_call
        + "    sys.stdout.write(json.dumps(out))\n\n"
        + "_main()\n"
    )


def parse_result(stdout: str) -> Optional[dict]:
    if not stdout:
        return None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and "w" in obj:
            return obj
        return None
    return None


# ==============================================================================
# 2. ONNX EXPORT (from onnx_export)
# ==============================================================================

def _build_cnn_onnx(artifact: dict) -> bytes:
    params = artifact.get("w") or []
    arch = artifact.get("arch") or {}
    f = int(arch.get("F", 8))
    side = int(arch.get("side", 0))
    c = int(arch.get("C", 0))
    if not params or side <= 0 or c <= 0:
        raise ValueError("CNN artifact missing weights or arch (F/side/C)")
    oh = side - 2
    ph = oh // 2
    flat = f * ph * ph
    fc_base = f * 9 + f
    expected = f * 9 + f + flat * c + c
    if len(params) != expected:
        raise ValueError(f"CNN weight vector length {len(params)} != expected {expected}")

    conv_w = [float(v) for v in params[0:f * 9]]
    conv_b = [float(v) for v in params[f * 9:f * 9 + f]]
    fc_w = [float(v) for v in params[fc_base:fc_base + flat * c]]
    fc_b = [float(v) for v in params[fc_base + flat * c:]]

    images = helper.make_tensor_value_info("images", TensorProto.FLOAT, [None, 1, side, side])
    probs = helper.make_tensor_value_info("probabilities", TensorProto.FLOAT, [None, c])
    inits = [
        helper.make_tensor("conv_w", TensorProto.FLOAT, [f, 1, 3, 3], conv_w),
        helper.make_tensor("conv_b", TensorProto.FLOAT, [f], conv_b),
        helper.make_tensor("fc_w", TensorProto.FLOAT, [flat, c], fc_w),
        helper.make_tensor("fc_b", TensorProto.FLOAT, [c], fc_b),
    ]
    nodes = [
        helper.make_node("Conv", ["images", "conv_w", "conv_b"], ["conv"],
                         kernel_shape=[3, 3], pads=[0, 0, 0, 0], strides=[1, 1]),
        helper.make_node("Relu", ["conv"], ["relu"]),
        helper.make_node("MaxPool", ["relu"], ["pool"], kernel_shape=[2, 2], strides=[2, 2]),
        helper.make_node("Flatten", ["pool"], ["flat"], axis=1),
        helper.make_node("MatMul", ["flat", "fc_w"], ["mm"]),
        helper.make_node("Add", ["mm", "fc_b"], ["logits"]),
        helper.make_node("Softmax", ["logits"], ["probabilities"], axis=1),
    ]
    graph = helper.make_graph(nodes, "cnn_model", [images], [probs], initializer=inits)
    model = helper.make_model(
        graph, producer_name="distributed-ai-platform",
        opset_imports=[helper.make_opsetid("", 13)],
    )
    entry = model.metadata_props.add()
    entry.key, entry.value = "model_type", "cnn"
    entry2 = model.metadata_props.add()
    entry2.key, entry2.value = "output_semantics", "class_probabilities"
    onnx.checker.check_model(model)
    return model.SerializeToString()


def build_onnx_model(model_type: str, weights_json: str) -> bytes:
    if model_type not in {"linear_regression", "logistic_regression", "cnn"}:
        raise ValueError(f"Unsupported model_type: {model_type}")

    artifact = json.loads(weights_json)
    if model_type == "cnn":
        return _build_cnn_onnx(artifact)

    weights = artifact.get("w")
    if not isinstance(weights, list) or not weights:
        raise ValueError("Training weights are missing")
    bias = float(artifact.get("b", 0.0))
    feature_names = artifact.get("feature_names", [])
    feature_count = len(weights)

    input_info = helper.make_tensor_value_info(
        "features", TensorProto.FLOAT, [None, feature_count]
    )
    output_info = helper.make_tensor_value_info(
        "prediction", TensorProto.FLOAT, [None, 1]
    )
    weight_tensor = helper.make_tensor(
        "weights",
        TensorProto.FLOAT,
        [feature_count, 1],
        [float(value) for value in weights],
    )
    bias_tensor = helper.make_tensor("bias", TensorProto.FLOAT, [1], [bias])

    nodes = [
        helper.make_node("MatMul", ["features", "weights"], ["linear_score"]),
        helper.make_node("Add", ["linear_score", "bias"], ["score"]),
    ]
    if model_type == "logistic_regression":
        nodes.append(helper.make_node("Sigmoid", ["score"], ["prediction"]))
    else:
        nodes.append(helper.make_node("Identity", ["score"], ["prediction"]))

    graph = helper.make_graph(
        nodes,
        f"{model_type}_model",
        [input_info],
        [output_info],
        initializer=[weight_tensor, bias_tensor],
    )
    model = helper.make_model(
        graph,
        producer_name="distributed-ai-platform",
        opset_imports=[helper.make_opsetid("", 13)],
    )
    metadata = {
        "model_type": model_type,
        "feature_names": json.dumps(feature_names),
        "output_semantics": "sigmoid_probability" if model_type == "logistic_regression" else "regression_value",
    }
    for key, value in metadata.items():
        entry = model.metadata_props.add()
        entry.key = key
        entry.value = value
    onnx.checker.check_model(model)
    return model.SerializeToString()


# ==============================================================================
# 3. TRAINING ORCHESTRATOR & FEDAVG (from training_orchestrator)
# ==============================================================================

async def _update_run(run_id: uuid.UUID, **fields) -> None:
    db = get_db_manager()
    async with db.session() as session:
        run = (
            await session.execute(select(TrainingRun).where(TrainingRun.id == run_id))
        ).scalar_one_or_none()
        if not run:
            return
        for key, value in fields.items():
            setattr(run, key, value)
        session.add(run)


def _storage_headers(owner_id: Optional[str]) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {create_internal_token('ssh-executor')}"}
    if owner_id:
        headers["X-On-Behalf-Of"] = str(owner_id)
    return headers


def _read_rows(
    metadata: dict,
    target_column: str,
    feature_columns: list[str],
    settings,
    owner_id: Optional[str] = None,
) -> tuple[list[list[float]], list[str]]:
    dataset_id = metadata.get("id")
    if not dataset_id:
        raise ValueError("Dataset metadata does not contain an ID")
    file_format = (metadata.get("file_format") or "csv").lower()
    if file_format not in ("csv", "json", "jsonl"):
        raise ValueError(
            f"{file_format} datasets need the Spark + Ray engine; the Python engine reads CSV and JSON"
        )
    download_url = (
        f"{settings.storage_service_url}/api/v1/storage/datasets/{dataset_id}/download"
    )
    with httpx.Client(timeout=300.0) as client:
        resp = client.get(download_url, headers=_storage_headers(owner_id))
        _check_storage_response(resp)
        raw_bytes = resp.content

    text = raw_bytes.decode("utf-8", errors="replace")

    if file_format in ("json", "jsonl"):
        records: list[dict] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not records:
            try:
                records = json.loads(text)
            except json.JSONDecodeError:
                pass
        if not records:
            raise ValueError("JSON dataset is empty or unparseable")
        columns = list(records[0].keys())
    else:
        reader = csv.DictReader(io.StringIO(text))
        records = []
        columns_seen: list[str] | None = None
        for row in reader:
            if columns_seen is None:
                columns_seen = list(row.keys())
            records.append(dict(row))
            if len(records) >= settings.max_train_rows:
                break
        columns = columns_seen or []

    if not records:
        raise ValueError("Dataset is empty")

    if target_column not in columns:
        raise ValueError(f"Target column '{target_column}' not found in dataset")

    if feature_columns:
        missing = [c for c in feature_columns if c not in columns]
        if missing:
            raise ValueError(f"Feature columns not found: {', '.join(missing)}")
        selected_features = feature_columns
    else:
        first = records[0]
        selected_features = []
        for name in columns:
            if name == target_column:
                continue
            try:
                float(first.get(name))
                selected_features.append(name)
            except (TypeError, ValueError):
                continue

    if not selected_features:
        raise ValueError("No numeric feature columns available")

    names = [*selected_features, target_column]
    rows: list[list[float]] = []
    for record in records[: settings.max_train_rows]:
        values = [record.get(name) for name in names]
        if any(v is None for v in values):
            continue
        try:
            rows.append([float(v) for v in values])
        except (TypeError, ValueError):
            continue

    if not rows:
        raise ValueError("No valid numeric rows found in dataset")
    return rows, selected_features


async def _load_dataset_rows(
    dataset_id: uuid.UUID,
    target_column: str,
    feature_columns: list[str],
    settings,
    num_shards: int,
    master_engine: str = "python",
    owner_id: Optional[str] = None,
) -> tuple[list[list[float]], list[str]]:
    metadata = await _fetch_metadata(dataset_id, settings, owner_id)
    return await asyncio.to_thread(
        _read_rows, metadata, target_column, feature_columns, settings, owner_id
    )


def _local_worker(args):
    model_type, lr, epochs, weights, rows = args
    if model_type == "cnn":
        return _train_cnn(weights, rows, lr, epochs)
    return train_core(model_type, lr, epochs, weights, rows)


def _run_local(model_type, lr, epochs, weights, shards, local_cores):
    jobs = [(model_type, lr, epochs, weights, shard) for shard in shards]
    workers = max(1, min(local_cores, len(jobs)))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_local_worker, jobs))


def node_score(
    cores: int,
    gpu_memory_gb: float,
    *,
    job_uses_gpu: bool,
    gpu_enforceable: bool,
    w_gpu: float = 5.0,
    w_cpu: float = 1.0,
) -> float:
    score = max(0, int(cores or 0)) * max(0.0, float(w_cpu))
    if job_uses_gpu and gpu_enforceable:
        score += max(0.0, float(gpu_memory_gb or 0.0)) * max(0.0, float(w_gpu))
    return score


def largest_remainder(total: int, weights: list[float]) -> list[int]:
    n = len(weights)
    total = max(0, int(total))
    if n == 0:
        return []
    ws = [max(0.0, float(w)) for w in weights]
    if sum(ws) <= 0:
        ws = [1.0] * n
    s = sum(ws)
    exact = [total * w / s for w in ws]
    counts = [int(x) for x in exact]
    rest = total - sum(counts)
    order = sorted(range(n), key=lambda i: (-(exact[i] - counts[i]), i))
    for i in order[:rest]:
        counts[i] += 1
    if total >= n:
        for i in range(n):
            if counts[i] == 0:
                donor = max(range(n), key=lambda j: (counts[j], -j))
                counts[donor] -= 1
                counts[i] += 1
    return counts


def interleave_split(rows: list, sizes: list[int]) -> list[list]:
    n = len(rows)
    if sum(sizes) != n:
        raise ValueError("shard sizes must sum to the number of rows")
    shards: list[list] = [[] for _ in sizes]
    for j, row in enumerate(rows):
        best, best_gap = -1, None
        for i, size in enumerate(sizes):
            if len(shards[i]) >= size:
                continue
            gap = size * (j + 1) / n - len(shards[i])
            if best_gap is None or gap > best_gap + 1e-12:
                best, best_gap = i, gap
        shards[best].append(row)
    return shards


def plan_weighted_shards(
    rows: list,
    nodes: list[dict],
    *,
    job_uses_gpu: bool,
    w_gpu: float = 5.0,
    w_cpu: float = 1.0,
) -> tuple[list[list], list]:
    if not nodes:
        return [], []
    scores = [
        node_score(
            n.get("cores", 0), n.get("gpu_memory_gb", 0.0),
            job_uses_gpu=job_uses_gpu, gpu_enforceable=bool(n.get("gpu_enforceable")),
            w_gpu=w_gpu, w_cpu=w_cpu,
        )
        for n in nodes
    ]
    per_node = largest_remainder(len(rows), scores)
    sizes: list[int] = []
    affinity: list = []
    for node, count in zip(nodes, per_node):
        slots = max(1, int(node.get("slots") or 1))
        for part in largest_remainder(count, [1.0] * slots):
            sizes.append(part)
            affinity.append(node.get("node_id"))
    shards = interleave_split(rows, sizes)
    kept = [(s, a) for s, a in zip(shards, affinity) if s]
    return [s for s, _ in kept], [a for _, a in kept]


def allocation_shard_inputs(allocations: list) -> list[dict]:
    out = []
    for alloc in allocations:
        node = alloc.node
        gpu_gb = 0.0
        if node_gpu_fraction(node) > 0 and getattr(alloc, "gpu_allocated", 0):
            gpu_gb = float(getattr(node, "allocated_gpu_memory_gb", 0.0) or 0.0) * int(
                alloc.gpu_allocated
            )
        out.append({
            "node_id": node.id,
            "cores": int(alloc.cpu_allocated),
            "slots": int(alloc.slots),
            "gpu_memory_gb": gpu_gb,
            "gpu_enforceable": node_gpu_enforceable(node),
        })
    return out


def _cnn_arch(rows: list, num_classes: int = 0) -> dict:
    d = len(rows[0]) - 1
    side = int(d ** 0.5)
    side -= side % 2
    if side < 4:
        side = 4
    if num_classes and num_classes > 0:
        c = num_classes
    else:
        c = int(max(int(round(float(r[d]))) for r in rows)) + 1
    return {"F": 8, "side": side, "C": max(2, c)}


def build_train_data(run, rows, feature_names, hyper, metrics, history) -> dict:
    target = (hyper or {}).get("target_column") or "target"
    return {
        "run_id": str(run.id),
        "dataset_id": str(run.dataset_id),
        "model_type": run.model_type,
        "engine": getattr(run, "engine", None),
        "mode": run.mode,
        "rounds": run.rounds,
        "hyperparameters": hyper,
        "feature_names": feature_names,
        "target_column": target,
        "columns": feature_names + [target],
        "num_rows": len(rows),
        "metrics": metrics,
        "history": history,
        "rows": rows,
    }


async def _online_nodes(owner_id: str, node_ids: list | None = None) -> list:
    db = get_db_manager()
    async with db.session() as session:
        stmt = select(SSHNode).where(
            SSHNode.owner_id == owner_id, SSHNode.status == "online"
        )
        if node_ids:
            stmt = stmt.where(SSHNode.id.in_(list(node_ids)))
        return list((await session.execute(stmt)).scalars().all())


# --- model families ------------------------------------------------------
CUSTOM_MODEL = "custom"
SPARK_PREFIX = "spark:"


def model_family(model_type: str) -> str:
    """"builtin" (stdlib trainers), "custom" (user model.py) or "spark" (MLlib)."""
    if model_type in SUPPORTED_MODELS:
        return "builtin"
    if model_type == CUSTOM_MODEL:
        return "custom"
    if model_type.startswith(SPARK_PREFIX) and len(model_type) > len(SPARK_PREFIX):
        return "spark"
    raise ValueError(f"Unsupported model_type: {model_type}")


def _builtin_pairs(results: list) -> list[tuple[dict, int]]:
    """Built-in shard results -> (params, rows) pairs for average_params."""
    pairs = []
    for r in results:
        if r and r.get("w"):
            params = {k: r[k] for k in ("w", "b", "arch") if k in r}
            pairs.append((params, int(r.get("n", 0) or 0)))
    return pairs


def aggregate(results: list) -> Optional[dict]:
    """Row-weighted FedAvg of built-in shard results ({"w", "b", "n", ...})."""
    return average_params(_builtin_pairs(results))


def _short_error(exc: BaseException) -> str:
    """One readable line, also for Py4J errors that wrap a Java stack trace."""
    text = str(exc).strip()
    for line in text.splitlines():
        line = line.strip(": \t")
        if ("Exception:" in line or "Error:" in line) and not line.startswith("at "):
            return line[:500]
    return (text.splitlines()[0] if text else type(exc).__name__)[:500]


def _stderr_tail(result: Optional[dict], limit: int = 600) -> str:
    text = ((result or {}).get("stderr") or "").strip()
    return text[-limit:] if text else "no error output"


@dataclass
class _RunContext:
    run_id: uuid.UUID
    owner_id: str
    model_type: str
    mode: str
    rounds: int
    lr: float
    epochs: int
    local_cores: int
    num_classes: int
    engine: str
    hyper: dict
    settings: SSHExecutorSettings
    execution_policy: str
    rows: list = field(default_factory=list)
    feature_names: list = field(default_factory=list)
    shards: list = field(default_factory=list)
    affinity: Optional[list] = None
    allocations: list = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def nice(self) -> int:
        return policy_nice(self.execution_policy, self.settings.task_nice)


# --- dataset access ------------------------------------------------------
def _check_storage_response(resp: httpx.Response) -> None:
    """Raise with storage-service's own message (e.g. "stored in HDFS, which is
    not running") instead of a bare HTTP status."""
    if not resp.is_error:
        return
    try:
        message = resp.json().get("message")
    except ValueError:
        message = None
    if message:
        raise RuntimeError(f"Could not read the dataset: {message}")
    resp.raise_for_status()


async def _fetch_metadata(dataset_id, settings, owner_id: Optional[str]) -> dict:
    url = f"{settings.storage_service_url}/api/v1/storage/datasets/{dataset_id}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.get(url, headers=_storage_headers(owner_id))
        _check_storage_response(resp)
        return resp.json()


def _download_to_file(metadata: dict, settings, owner_id: Optional[str]) -> str:
    """Stream a dataset to a temp file (for Spark, which reads files)."""
    fmt = (metadata.get("file_format") or "csv").lower()
    url = f"{settings.storage_service_url}/api/v1/storage/datasets/{metadata['id']}/download"
    fd, path = tempfile.mkstemp(prefix="dp-dataset-", suffix=f".{fmt}")
    try:
        with os.fdopen(fd, "wb") as out, httpx.Client(timeout=600.0) as client:
            with client.stream("GET", url, headers=_storage_headers(owner_id)) as resp:
                if resp.is_error:
                    resp.read()
                _check_storage_response(resp)
                for chunk in resp.iter_bytes(1024 * 1024):
                    out.write(chunk)
    except BaseException:
        _remove_quietly(path)
        raise
    return path


def _remove_quietly(path: Optional[str]) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


async def _load_rows_spark(ctx: _RunContext, dataset_id, target_column, feature_columns):
    metadata = await _fetch_metadata(dataset_id, ctx.settings, ctx.owner_id)
    path = await asyncio.to_thread(_download_to_file, metadata, ctx.settings, ctx.owner_id)
    try:
        def _read():
            from ml.spark_pipeline import dataframe_rows, read_dataframe

            df = read_dataframe(path, metadata.get("file_format") or "csv")
            return dataframe_rows(
                df, target_column, feature_columns, ctx.settings.max_train_rows,
                seed=int(ctx.hyper.get("seed", 42)),
            )
        rows, features, data_details = await asyncio.to_thread(_read)
    finally:
        _remove_quietly(path)
    ctx.details["data"] = {"loader": "spark", **data_details}
    return rows, features


async def export_rows(dataset_id, owner_id: str, hyper: dict) -> tuple[list, list]:
    """The numeric rows a run trains on, read with the loader of its engine."""
    settings = get_settings()
    target = hyper.get("target_column", "")
    features = hyper.get("feature_columns", []) or []
    if (hyper.get("engine") or PYTHON) == SPARK_RAY:
        await asyncio.to_thread(require_engine, SPARK_RAY)
        ctx = _RunContext(
            run_id=uuid.uuid4(), owner_id=owner_id, model_type="", mode="", rounds=0,
            lr=0.0, epochs=0, local_cores=1, num_classes=0, engine=SPARK_RAY, hyper=hyper,
            settings=settings, execution_policy="safe_background",
        )
        return await _load_rows_spark(ctx, dataset_id, target, features)
    return await _load_dataset_rows(dataset_id, target, features, settings, 1, PYTHON, owner_id=owner_id)


# --- entry point ---------------------------------------------------------
async def run_training(run_id: uuid.UUID) -> None:
    settings = get_settings()
    db = get_db_manager()

    async with db.session() as session:
        run = (
            await session.execute(select(TrainingRun).where(TrainingRun.id == run_id))
        ).scalar_one_or_none()
        if not run:
            logger.error("training_run_missing", run_id=str(run_id))
            return
        owner_id = run.owner_id
        dataset_id = run.dataset_id
        model_type = run.model_type
        mode = run.mode
        rounds = run.rounds
        model_code = getattr(run, "model_code", None)
        hyper = json.loads(run.hyperparams_json or "{}")

    engine = hyper.get("engine") or PYTHON
    ctx = _RunContext(
        run_id=run_id, owner_id=owner_id, model_type=model_type, mode=mode,
        rounds=int(rounds), lr=float(hyper.get("learning_rate", 0.01)),
        epochs=int(hyper.get("epochs", 10)), local_cores=int(hyper.get("local_cores", 2)),
        num_classes=int(hyper.get("num_classes", 0) or 0), engine=engine, hyper=hyper,
        settings=settings, execution_policy=hyper.get("execution_policy") or "safe_background",
    )
    ctx.details["engine"] = engine
    target_column = hyper.get("target_column", "")
    feature_columns = hyper.get("feature_columns", []) or []
    node_ids: list = []
    for _nid in (hyper.get("node_ids") or []):
        try:
            node_ids.append(uuid.UUID(str(_nid)))
        except (ValueError, TypeError):
            pass

    await _update_run(run_id, status="running")

    try:
        family = model_family(model_type)
        if engine == SPARK_RAY:
            await asyncio.to_thread(require_engine, SPARK_RAY)
        if family == "spark" and engine != SPARK_RAY:
            raise ValueError("Spark MLlib models need the Spark + Ray engine")
        if family == "custom":
            if mode != "multi":
                raise ValueError("Custom models train on worker nodes; use mode 'multi'")
            if not model_code:
                raise ValueError("Custom model code is missing")
    except (ValueError, EngineUnavailable) as exc:
        await _update_run(run_id, status="failed", message=str(exc), details_json=json.dumps(ctx.details))
        return

    if family == "spark":
        await _run_spark_model(ctx, dataset_id, target_column, feature_columns)
        return

    nodes: list = []
    if mode == "multi":
        nodes = await _online_nodes(owner_id, node_ids)
        if not nodes:
            await _update_run(
                run_id, status="failed", message="No online provider nodes available"
            )
            return

    try:
        if engine == SPARK_RAY:
            rows, feature_names = await _load_rows_spark(ctx, dataset_id, target_column, feature_columns)
        else:
            rows, feature_names = await _load_dataset_rows(
                dataset_id, target_column, feature_columns, settings,
                max(1, ctx.local_cores), engine, owner_id=owner_id,
            )
    except Exception as exc:
        logger.error("training_load_failed", run_id=str(run_id), error=str(exc))
        await _update_run(
            run_id, status="failed", message=f"Dataset load failed: {_short_error(exc)}",
            details_json=json.dumps(ctx.details),
        )
        return
    ctx.rows, ctx.feature_names = rows, feature_names

    if mode == "multi":
        ctx.allocations = await reserve_allocations(
            owner_id, nodes, job_id=str(run_id), cpu_per_task=1,
            ttl_seconds=settings.lease_ttl_seconds,
        )
        if not ctx.allocations:
            await _update_run(
                run_id, status="failed",
                message="No free capacity: every selected node is fully leased",
            )
            return
        uses_gpu = REMOTE_SCRIPT_USES_GPU if family == "builtin" else bool(hyper.get("uses_gpu"))
        ctx.shards, ctx.affinity = plan_weighted_shards(
            rows, allocation_shard_inputs(ctx.allocations),
            job_uses_gpu=uses_gpu,
            w_gpu=settings.shard_weight_gpu, w_cpu=settings.shard_weight_cpu,
        )
    else:
        k = max(1, min(max(1, ctx.local_cores), len(rows)))
        ctx.shards = [s for s in interleave_split(rows, largest_remainder(len(rows), [1.0] * k)) if s]

    try:
        if family == "custom":
            await _train_custom_rounds(ctx, model_code)
        else:
            await _train_rounds(ctx)
    finally:
        await release_allocations(owner_id, ctx.allocations)


# --- built-in models -----------------------------------------------------
async def _train_rounds(ctx: _RunContext) -> None:
    """Federated rounds for the built-in models (python or spark_ray engine)."""
    settings = ctx.settings
    model_type, rows, shards = ctx.model_type, ctx.rows, ctx.shards
    cnn_arch = _cnn_arch(rows, ctx.num_classes) if model_type == "cnn" else None
    weights: Optional[dict] = None
    if cnn_arch is not None:
        weights = {"w": _cnn_init(cnn_arch), "b": 0.0, "arch": cnn_arch}

    server = None
    history: list = []
    shard_meta: list = []
    try:
        if ctx.engine == SPARK_RAY:
            from ml.ray_orchestrator import ParameterServer

            server = await asyncio.to_thread(ParameterServer, weights)
        for rnd in range(ctx.rounds):
            if ctx.mode == "multi":
                code = generate_remote_script(model_type, ctx.lr, ctx.epochs)
                payloads = [json.dumps({"weights": weights, "rows": s}) for s in shards]
                async with lease_heartbeat(ctx.owner_id, ctx.allocations, settings.lease_ttl_seconds):
                    results = await run_across_nodes(
                        ctx.allocations, code, payloads,
                        nice=ctx.nice,
                        timeout_seconds=settings.training_timeout_seconds,
                        max_output_bytes=settings.max_train_output_bytes,
                        affinity=ctx.affinity,
                        sweep_minutes=settings.stale_sandbox_minutes,
                    )
                shard_results = [parse_result((r or {}).get("stdout", "")) for r in results]
                if rnd == ctx.rounds - 1:
                    shard_meta = [
                        {
                            "shard": i,
                            "rows": len(shards[i]),
                            "node_id": str(r["node_id"]) if r and r.get("node_id") else None,
                            "status": (r or {}).get("status"),
                            "attempts": (r or {}).get("attempts"),
                        }
                        for i, r in enumerate(results)
                    ]
            else:
                if ctx.engine == SPARK_RAY:
                    from ml.ray_orchestrator import train_shards_parallel

                    shard_results = await asyncio.to_thread(
                        train_shards_parallel, builtin_helpers_source(), model_type,
                        ctx.lr, ctx.epochs, weights, shards,
                    )
                    where = "ray"
                else:
                    shard_results = await asyncio.to_thread(
                        _run_local, model_type, ctx.lr, ctx.epochs, weights, shards, ctx.local_cores
                    )
                    where = "local"
                if rnd == ctx.rounds - 1:
                    shard_meta = [
                        {"shard": i, "rows": len(shards[i]), "node_id": where, "status": "completed"}
                        for i in range(len(shards))
                    ]

            pairs = _builtin_pairs(shard_results)
            if server is not None:
                agg = await asyncio.to_thread(server.apply, pairs)
            else:
                agg = await asyncio.to_thread(average_params, pairs)
            if agg is None:
                await _update_run(
                    ctx.run_id, status="failed", message="All shards failed to produce weights",
                    details_json=json.dumps(ctx.details),
                )
                return
            weights = agg
            if cnn_arch is not None:
                weights["arch"] = cnn_arch
            round_metrics = evaluate(model_type, weights, rows)
            history.append({"round": rnd + 1, "metrics": round_metrics})
            await _update_run(
                ctx.run_id,
                metrics_json=json.dumps(round_metrics),
                history_json=json.dumps(history),
            )
        if server is not None:
            ctx.details["ray"] = await asyncio.to_thread(server.stats)
    except Exception as exc:
        logger.error("training_failed", run_id=str(ctx.run_id), error=str(exc))
        await _update_run(
            ctx.run_id, status="failed", message=f"Training failed: {_short_error(exc)}",
            details_json=json.dumps(ctx.details),
        )
        return
    finally:
        if server is not None:
            await asyncio.to_thread(server.close)

    final_metrics = evaluate(model_type, weights, rows)
    await _update_run(
        ctx.run_id,
        status="completed",
        metrics_json=json.dumps(final_metrics),
        weights_json=json.dumps({**(weights or {}), "feature_names": ctx.feature_names}),
        history_json=json.dumps(history),
        shards_json=json.dumps(shard_meta),
        details_json=json.dumps(ctx.details),
        message=f"Trained on {len(rows)} rows across {len(shards)} shard(s) [{ctx.engine}]",
    )
    logger.info(
        "training_done", run_id=str(ctx.run_id), engine=ctx.engine, mode=ctx.mode,
        shards=len(shards), rows=len(rows),
    )


# --- custom models -------------------------------------------------------
async def _run_custom(ctx: _RunContext, model_code: Optional[str], payloads: list[str],
                      affinity: Optional[list], timeout: Optional[int] = None) -> list[dict]:
    files = custom_model.files(model_code or "")
    async with lease_heartbeat(ctx.owner_id, ctx.allocations, ctx.settings.lease_ttl_seconds):
        return await run_across_nodes(
            ctx.allocations, custom_model.RUNNER_SOURCE, payloads,
            nice=ctx.nice,
            timeout_seconds=timeout or ctx.settings.training_timeout_seconds,
            max_output_bytes=ctx.settings.max_train_output_bytes,
            files_for=lambda _i: files,
            affinity=affinity,
            sweep_minutes=ctx.settings.stale_sandbox_minutes,
        )


def _node_label(ctx: _RunContext, node_id) -> str:
    for alloc in ctx.allocations:
        if alloc.node.id == node_id:
            return getattr(alloc.node, "name", None) or str(node_id)
    return str(node_id)


async def _train_custom_rounds(ctx: _RunContext, model_code: Optional[str]) -> None:
    """Federated rounds for a user model.py run on the worker nodes."""
    settings = ctx.settings
    model_params = ctx.hyper.get("model_params") or {}
    base_config = {
        **model_params,
        "feature_names": ctx.feature_names,
        "target_column": ctx.hyper.get("target_column", ""),
        "learning_rate": ctx.lr,
        "epochs": ctx.epochs,
        "rounds": ctx.rounds,
        "seed": int(ctx.hyper.get("seed", 42)),
    }

    async def fail(message: str) -> None:
        await _update_run(ctx.run_id, status="failed", message=message, details_json=json.dumps(ctx.details))

    server = None
    history: list = []
    try:
        # 1. Every node can import the model and has train().
        node_ids = []
        for alloc in ctx.allocations:
            if alloc.node.id not in node_ids:
                node_ids.append(alloc.node.id)
        checks = await _run_custom(
            ctx, model_code, [custom_model.payload("check", config=base_config)] * len(node_ids),
            node_ids, timeout=min(120, settings.training_timeout_seconds),
        )
        problems, has_evaluate, has_init = [], False, False
        for nid, result in zip(node_ids, checks):
            parsed = custom_model.parse_output((result or {}).get("stdout", ""))
            if not parsed:
                problems.append(f"{_node_label(ctx, nid)}: {_stderr_tail(result, 300)}")
            elif not parsed.get("ok"):
                problems.append(f"{_node_label(ctx, nid)}: model.py has no train() function")
            else:
                has_evaluate = bool(parsed.get("has_evaluate"))
                has_init = bool(parsed.get("has_init"))
        if problems:
            await fail("Model check failed on " + "; ".join(problems))
            return
        ctx.details["custom"] = {"has_init": has_init, "has_evaluate": has_evaluate}

        # 2. One shared starting point.
        params = None
        if has_init:
            (init_result,) = await _run_custom(
                ctx, model_code, [custom_model.payload("init", config=base_config)], None,
                timeout=min(300, settings.training_timeout_seconds),
            )
            parsed = custom_model.parse_output((init_result or {}).get("stdout", ""))
            if not parsed:
                await fail(f"init() failed: {_stderr_tail(init_result)}")
                return
            params = parsed.get("params")

        if ctx.engine == SPARK_RAY:
            from ml.ray_orchestrator import ParameterServer

            server = await asyncio.to_thread(ParameterServer, params)

        # 3. Rounds. Each task first evaluates the incoming global model, so
        #    metrics for round r arrive with round r+1 (no extra trip).
        shard_meta: list = []
        for rnd in range(1, ctx.rounds + 1):
            config = {**base_config, "round": rnd}
            payloads = [custom_model.payload("train", params=params, rows=s, config=config) for s in ctx.shards]
            results = await _run_custom(ctx, model_code, payloads, ctx.affinity)
            parsed = [custom_model.parse_output((r or {}).get("stdout", "")) for r in results]
            pairs = [(p.get("params"), int(p.get("n", 0))) for p in parsed if p and p.get("params") is not None]
            if not pairs:
                first_failed = next((r for r, p in zip(results, parsed) if not p), results[0] if results else None)
                await fail(f"Round {rnd}: every shard failed. Last error: {_stderr_tail(first_failed)}")
                return
            if rnd > 1 and has_evaluate:
                metrics = combine_metrics([(p.get("metrics"), p.get("n", 0)) for p in parsed if p])
                history.append({"round": rnd - 1, "metrics": metrics})
            if server is not None:
                params = await asyncio.to_thread(server.apply, pairs)
            else:
                params = await asyncio.to_thread(average_params, pairs)
            shard_meta = [
                {
                    "shard": i,
                    "rows": len(ctx.shards[i]),
                    "node_id": str(r["node_id"]) if r and r.get("node_id") else None,
                    "status": (r or {}).get("status") if p else "failed",
                    "attempts": (r or {}).get("attempts"),
                    **({} if p else {"error": _stderr_tail(r, 300)}),
                }
                for i, (r, p) in enumerate(zip(results, parsed))
            ]
            await _update_run(
                ctx.run_id,
                history_json=json.dumps(history),
                metrics_json=json.dumps(history[-1]["metrics"]) if history else None,
                shards_json=json.dumps(shard_meta),
            )

        # 4. Final metrics on the final model.
        final_metrics: dict = {"n": len(ctx.rows)}
        if has_evaluate:
            payloads = [custom_model.payload("evaluate", params=params, rows=s, config=base_config) for s in ctx.shards]
            results = await _run_custom(ctx, model_code, payloads, ctx.affinity)
            parsed = [custom_model.parse_output((r or {}).get("stdout", "")) for r in results]
            final_metrics = combine_metrics([(p.get("metrics"), p.get("n", 0)) for p in parsed if p]) or final_metrics
            history.append({"round": ctx.rounds, "metrics": final_metrics})
        if server is not None:
            ctx.details["ray"] = await asyncio.to_thread(server.stats)
    except Exception as exc:
        logger.error("custom_training_failed", run_id=str(ctx.run_id), error=str(exc))
        await fail(f"Training failed: {_short_error(exc)}")
        return
    finally:
        if server is not None:
            await asyncio.to_thread(server.close)

    await _update_run(
        ctx.run_id,
        status="completed",
        metrics_json=json.dumps(final_metrics),
        weights_json=json.dumps({"params": params, "feature_names": ctx.feature_names}),
        history_json=json.dumps(history),
        shards_json=json.dumps(shard_meta),
        details_json=json.dumps(ctx.details),
        message=(
            f"Trained a custom model on {len(ctx.rows)} rows across "
            f"{len(ctx.shards)} shard(s) [{ctx.engine}]"
        ),
    )
    logger.info("custom_training_done", run_id=str(ctx.run_id), engine=ctx.engine, shards=len(ctx.shards))


# --- Spark MLlib models --------------------------------------------------
async def _run_spark_model(ctx: _RunContext, dataset_id, target_column: str, feature_columns: list) -> None:
    """Train any Spark MLlib estimator with Spark on the master (no SSH workers)."""
    settings = ctx.settings
    path = None
    try:
        metadata = await _fetch_metadata(dataset_id, settings, ctx.owner_id)
        path = await asyncio.to_thread(_download_to_file, metadata, settings, ctx.owner_id)

        def _fit():
            from ml.spark_ml import train_spark_model
            from ml.spark_pipeline import read_dataframe

            df = read_dataframe(path, metadata.get("file_format") or "csv")
            return train_spark_model(
                df, ctx.model_type,
                target=target_column,
                feature_columns=feature_columns,
                params=ctx.hyper.get("model_params") or {},
                grid=ctx.hyper.get("param_grid") or {},
                test_fraction=float(ctx.hyper.get("test_fraction", 0.2)),
                max_rows=settings.max_spark_rows,
                seed=int(ctx.hyper.get("seed", 42)),
            )

        metrics, spark_details, artifact = await asyncio.to_thread(_fit)
    except Exception as exc:
        logger.error("spark_training_failed", run_id=str(ctx.run_id), error=str(exc))
        await _update_run(
            ctx.run_id, status="failed", message=f"Spark training failed: {_short_error(exc)}",
            details_json=json.dumps(ctx.details),
        )
        return
    finally:
        _remove_quietly(path)

    if len(artifact) > settings.max_model_artifact_bytes:
        await _update_run(
            ctx.run_id, status="failed",
            message=(
                f"The trained model is {len(artifact) / 1e6:.1f} MB, above the "
                f"{settings.max_model_artifact_bytes / 1e6:.0f} MB limit (MAX_MODEL_ARTIFACT_BYTES)"
            ),
        )
        return
    ctx.details["spark"] = spark_details
    await _update_run(
        ctx.run_id,
        status="completed",
        metrics_json=json.dumps(metrics),
        history_json=json.dumps([{"round": 1, "metrics": metrics}]),
        weights_json=json.dumps({"feature_names": spark_details["feature_names"], "model": spark_details["model"]}),
        shards_json=json.dumps([]),
        details_json=json.dumps(ctx.details),
        artifact=artifact,
        message=(
            f"Trained Spark {spark_details['model']} on {spark_details['rows_train']} rows "
            f"(tested on {spark_details['rows_test']}) [spark_ray]"
        ),
    )
    logger.info("spark_training_done", run_id=str(ctx.run_id), model=spark_details["model"])

"""routes — Consolidated API routers for ssh-executor.

Consolidated module combining:
- Shared DB sessions and sliding-window rate limiting (formerly routes_common.py)
- Provider node onboarding, lifecycle, and capacity routes (formerly routes_nodes.py)
- Language runtimes and parallel task batch routes (formerly routes_batches.py)
- Distributed ML training routes (formerly routes_train.py)
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import json
import time
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import desc, select
from sqlalchemy.orm import undefer
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database import get_db_manager
from projects import BundleError, BundleTooLarge
from tasks import TERMINAL_BATCH_STATUSES, BatchService, dispatch_batch, project_nodes, project_stores
from leases import LeaseService
import leases as execution_manager
from models import TrainingRun
from node_service import NodeService, NodeServiceError
from runtimes import gpu_fraction
import runtimes as runtime_registry
from schemas import (
    BatchCreateRequest,
    BatchListResponse,
    BatchResponse,
    BatchTasksResponse,
    BundleResponse,
    NodeConnectRequest,
    NodeConnectResponse,
    NodeDeleteResponse,
    NodeListResponse,
    NodeManualProvisionRequest,
    NodeManualProvisionResponse,
    NodeProvisionRequest,
    NodeProvisionResponse,
    NodeRegisterRequest,
    NodeResecureRequest,
    NodeResponse,
    NodeUpdateRequest,
    ProjectSpec,
    RuntimeInfo,
    RuntimeListResponse,
    TaskResponse,
    EngineListResponse,
    SparkModelListResponse,
    TrainRequest,
    TrainRunResponse,
)
from security import runtime_allowed, validate_execution_request
from shared.common.auth import get_current_user
from ssh import fingerprint
from ml.custom_model import STARTER_TEMPLATE, validate_model_code
from ml.engines import SPARK_RAY, EngineUnavailable, list_engines, require_engine
from training import build_onnx_model, build_train_data, export_rows, model_family, run_training

# ==============================================================================
# 1. SHARED DEPENDENCIES & RATE LIMITING
# ==============================================================================

async def get_db_session() -> AsyncSession:
    db_manager = get_db_manager()
    async with db_manager.session() as session:
        yield session


_RATE_WINDOW_SECONDS = 60
_rate_state: dict[tuple[str, str], deque] = defaultdict(deque)


def rate_limit(bucket: str, max_calls: int):
    async def _dep(user: dict = Depends(get_current_user)) -> None:
        owner = user.get("sub") or "anonymous"
        key = (bucket, owner)
        now = time.monotonic()
        window = _rate_state[key]
        while window and now - window[0] > _RATE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= max_calls:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({max_calls}/min); slow down",
            )
        window.append(now)

    return _dep


# ==============================================================================
# 2. NODES ROUTER (/nodes)
# ==============================================================================

nodes_router = APIRouter()


def get_node_service(session: AsyncSession = Depends(get_db_session)) -> NodeService:
    return NodeService(session)


def get_lease_service(session: AsyncSession = Depends(get_db_session)) -> LeaseService:
    return LeaseService(session)


def _node_response(node) -> NodeResponse:
    runtimes: dict[str, str] = {}
    if getattr(node, "runtimes_json", None):
        try:
            runtimes = json.loads(node.runtimes_json) or {}
        except (ValueError, TypeError):
            runtimes = {}
    return NodeResponse(
        id=node.id,
        owner_id=node.owner_id,
        name=node.name,
        host=node.host,
        port=node.port,
        username=node.username,
        auth_type=node.auth_type,
        fingerprint=node.host_key_b64 and node.host_key_type
        and fingerprint(node.host_key_type, node.host_key_b64),
        key_created_at=node.key_created_at,
        allocated_cpu=node.allocated_cpu,
        allocated_memory_gb=node.allocated_memory_gb,
        allocated_gpu_count=int(getattr(node, "allocated_gpu_count", 0) or 0),
        allocated_gpu_memory_gb=float(getattr(node, "allocated_gpu_memory_gb", 0.0) or 0.0),
        allocated_gpu_fraction=gpu_fraction(
            getattr(node, "allocated_gpu_count", 0),
            getattr(node, "allocated_gpu_memory_gb", 0.0),
            node.gpu_memory_gb,
        ),
        env_cache_gb=float(getattr(node, "env_cache_gb", 0.0) or 0.0),
        status=node.status,
        health_status=node.health_status,
        last_health_check=node.last_health_check,
        created_at=node.created_at,
        updated_at=node.updated_at,
        os_type=node.os_type,
        os_info=node.os_info,
        detected_cpu=node.detected_cpu,
        detected_memory_gb=node.detected_memory_gb,
        detected_gpu=node.detected_gpu,
        gpu_vendor=node.gpu_vendor,
        gpu_backend=node.gpu_backend,
        gpu_model=node.gpu_model,
        gpu_count=node.gpu_count,
        gpu_memory_gb=node.gpu_memory_gb,
        gpu_runtime=getattr(node, "gpu_runtime", None),
        python_version=node.python_version,
        runtimes=runtimes,
        execution_backend=node.execution_backend,
        resource_enforcement=node.resource_enforcement,
    )


@nodes_router.post(
    "/nodes/connect",
    response_model=NodeConnectResponse,
    dependencies=[Depends(rate_limit("connect", 30))],
)
async def connect_node(
    request: NodeConnectRequest,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        result = await service.connect(request, user.get("sub"))
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return NodeConnectResponse(**result)


@nodes_router.post(
    "/nodes/provision",
    response_model=NodeProvisionResponse,
    dependencies=[Depends(rate_limit("provision", 30))],
)
async def provision_node(
    request: NodeProvisionRequest,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        result = await service.provision(request, user.get("sub"))
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return NodeProvisionResponse(**result)


@nodes_router.post(
    "/nodes/provision/manual",
    response_model=NodeManualProvisionResponse,
    dependencies=[Depends(rate_limit("provision", 30))],
)
async def provision_node_manual(
    request: NodeManualProvisionRequest,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        result = await service.provision_manual(request, user.get("sub"))
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return NodeManualProvisionResponse(**result)


@nodes_router.post(
    "/nodes/provision/{provision_id}/verify",
    response_model=NodeProvisionResponse,
    dependencies=[Depends(rate_limit("provision", 30))],
)
async def verify_provision(
    provision_id: UUID,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        result = await service.verify_provision(provision_id, user.get("sub"))
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return NodeProvisionResponse(**result)


@nodes_router.post("/nodes", response_model=NodeResponse, status_code=201)
async def register_node(
    request: NodeRegisterRequest,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        node = await service.register(request, user.get("sub"))
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _node_response(node)


@nodes_router.get("/nodes", response_model=NodeListResponse)
async def list_nodes(
    skip: int = 0,
    limit: int = 100,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    nodes, total = await service.list_nodes(user.get("sub"), skip, limit)
    return NodeListResponse(items=[_node_response(n) for n in nodes], total=total)


@nodes_router.get("/nodes/{node_id}", response_model=NodeResponse)
async def get_node(
    node_id: UUID,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    node = await service.get_node(node_id, user.get("sub"))
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return _node_response(node)


@nodes_router.patch("/nodes/{node_id}", response_model=NodeResponse)
async def update_node(
    node_id: UUID,
    request: NodeUpdateRequest,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        node = await service.update_node(node_id, user.get("sub"), request)
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _node_response(node)


@nodes_router.delete("/nodes/{node_id}", response_model=NodeDeleteResponse)
async def delete_node(
    node_id: UUID,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        result = await service.delete_node(node_id, user.get("sub"))
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return NodeDeleteResponse(**result)


@nodes_router.post("/nodes/{node_id}/health-check", response_model=NodeResponse)
async def health_check_node(
    node_id: UUID,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        node = await service.health_check(node_id, user.get("sub"))
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _node_response(node)


@nodes_router.post("/nodes/{node_id}/refresh", response_model=NodeResponse)
async def refresh_node(
    node_id: UUID,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        node = await service.refresh(node_id, user.get("sub"))
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _node_response(node)


@nodes_router.post("/nodes/{node_id}/rotate-key", response_model=NodeResponse)
async def rotate_node_key(
    node_id: UUID,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        node = await service.rotate_key(node_id, user.get("sub"))
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _node_response(node)


@nodes_router.post("/nodes/{node_id}/resecure", response_model=NodeResponse)
async def resecure_node(
    node_id: UUID,
    request: NodeResecureRequest,
    user: dict = Depends(get_current_user),
    service: NodeService = Depends(get_node_service),
):
    try:
        node = await service.resecure(node_id, user.get("sub"), request)
    except NodeServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return _node_response(node)


@nodes_router.get("/nodes/{node_id}/capacity")
async def node_capacity(
    node_id: UUID,
    user: dict = Depends(get_current_user),
    service: LeaseService = Depends(get_lease_service),
):
    cap = await service.get_node_capacity(node_id, user.get("sub"))
    if cap is None:
        raise HTTPException(status_code=404, detail="Node not found")
    return cap


# ==============================================================================
# 3. BATCHES & RUNTIMES ROUTER (/batches, /runtimes)
# ==============================================================================

batches_router = APIRouter()
router = batches_router
BATCH_LIST_LIMIT = 20


def get_batch_service(session: AsyncSession = Depends(get_db_session)) -> BatchService:
    return BatchService(session)


@batches_router.get("/runtimes", response_model=RuntimeListResponse)
async def list_runtimes(
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    settings = get_settings()
    nodes = await execution_manager.online_nodes(session, user.get("sub"))
    items = []
    for rt in runtime_registry.list_runtimes():
        if not runtime_allowed(rt.id, settings.allowed_runtimes):
            continue
        items.append(
            RuntimeInfo(
                id=rt.id,
                name=rt.name,
                filename=rt.filename,
                kind=rt.kind,
                example_code=rt.example_code,
                windows_supported=rt.windows_supported,
                available_node_ids=[
                    n.id for n in nodes if execution_manager.node_has_runtime(n, rt.id)
                ],
            )
        )
    return RuntimeListResponse(items=items)


@batches_router.post(
    "/batches",
    response_model=BatchResponse,
    status_code=201,
    dependencies=[Depends(rate_limit("batch", 30))],
)
async def create_batch(
    request: BatchCreateRequest,
    background: BackgroundTasks,
    user: dict = Depends(get_current_user),
    service: BatchService = Depends(get_batch_service),
    session: AsyncSession = Depends(get_db_session),
):
    return await _start_batch(request, background, user, service, session)


async def _start_batch(
    request: BatchCreateRequest,
    background: BackgroundTasks,
    user: dict,
    service: BatchService,
    session: AsyncSession,
):
    settings = get_settings()
    if request.project is not None:
        await _check_project_request(request, user, session, settings)
    else:
        try:
            validate_execution_request(
                request.code,
                request.inputs,
                runtime=request.runtime,
                allowed_runtimes=settings.allowed_runtimes,
                custom_command=request.custom_command,
                custom_filename=request.custom_filename,
                timeout_seconds=request.timeout_seconds,
                max_timeout_seconds=settings.max_task_timeout_seconds,
                max_code_bytes=settings.max_code_bytes,
                max_inputs=settings.max_inputs_per_batch,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        nodes = await execution_manager.online_nodes(
            session, user.get("sub"), request.node_ids or None
        )
        if not execution_manager.select_runtime_nodes(nodes, request.runtime):
            raise HTTPException(
                status_code=400,
                detail=f"No selected online node has runtime '{request.runtime}'",
            )
    timeout_seconds = min(
        request.timeout_seconds or settings.task_timeout_seconds,
        settings.max_task_timeout_seconds,
    )
    batch = await service.create_batch(
        request, user.get("sub"), timeout_seconds=timeout_seconds
    )
    background.add_task(dispatch_batch, batch.id, request.node_ids or None)
    return batch


async def _check_project_request(request: BatchCreateRequest, user: dict, session: AsyncSession, settings) -> None:
    """A project needs its bundle on the master and a POSIX node with python3."""
    allowed = settings.allowed_runtimes
    if "*" not in allowed and "project" not in allowed:
        raise HTTPException(status_code=400, detail="Projects are not allowed on this platform (allowed_runtimes)")
    if len(request.inputs) > settings.max_inputs_per_batch:
        raise HTTPException(
            status_code=400,
            detail=f"Too many inputs ({len(request.inputs)} > {settings.max_inputs_per_batch})",
        )
    if request.timeout_seconds and request.timeout_seconds > settings.max_task_timeout_seconds:
        raise HTTPException(
            status_code=400,
            detail=f"timeout_seconds {request.timeout_seconds} exceeds the maximum of {settings.max_task_timeout_seconds}",
        )
    bundles, _outputs, _datasets = project_stores(settings)
    if not bundles.exists(user.get("sub"), request.project.bundle_id):
        raise HTTPException(status_code=400, detail="Unknown bundle_id: upload the project with POST /bundles first")
    nodes = await execution_manager.online_nodes(session, user.get("sub"), request.node_ids or None)
    if not project_nodes(nodes):
        raise HTTPException(
            status_code=400,
            detail="Projects need a Linux or macOS node with python3; none of the selected online nodes has both",
        )


@batches_router.post(
    "/bundles",
    response_model=BundleResponse,
    status_code=201,
    dependencies=[Depends(rate_limit("bundle", 60))],
)
async def upload_bundle(request: Request, user: dict = Depends(get_current_user)):
    """Store a project archive (.tar.gz, raw request body). Its id is its sha256."""
    settings = get_settings()
    bundles, _outputs, _datasets = project_stores(settings)
    try:
        info = await bundles.receive(
            user.get("sub"), request.stream(),
            max_bytes=settings.max_bundle_bytes,
            max_unpacked_bytes=settings.max_bundle_unpacked_bytes,
            max_members=settings.max_bundle_files,
        )
    except BundleError as exc:
        raise HTTPException(status_code=413 if isinstance(exc, BundleTooLarge) else 400, detail=str(exc))
    return BundleResponse(**info)


@batches_router.get("/bundles/{bundle_id}", response_model=BundleResponse)
async def get_bundle(bundle_id: str, user: dict = Depends(get_current_user)):
    """200 when this owner already uploaded the archive with that sha256."""
    bundles, _outputs, _datasets = project_stores(get_settings())
    if not bundles.exists(user.get("sub"), bundle_id):
        raise HTTPException(status_code=404, detail="Bundle not found")
    return BundleResponse(id=bundle_id, size_bytes=bundles.path(user.get("sub"), bundle_id).stat().st_size)


@batches_router.get("/batches/{batch_id}/tasks/{task_id}/outputs")
async def download_task_outputs(
    batch_id: UUID,
    task_id: UUID,
    user: dict = Depends(get_current_user),
    service: BatchService = Depends(get_batch_service),
):
    """The files a project task wrote to its output folders, as a .tar.gz."""
    batch = await service.get_batch(batch_id, user.get("sub"))
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    task = next((t for t in await service.list_tasks(batch_id) if t.id == task_id), None)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    _bundles, outputs, _datasets = project_stores(get_settings())
    path = outputs.path(str(task.id))
    if not task.output_files or not path.is_file():
        raise HTTPException(status_code=404, detail="This task brought back no output files")
    return FileResponse(
        path, media_type="application/gzip",
        filename=f"outputs-{str(batch.id)[:8]}-task{task.seq + 1}.tar.gz",
    )


@batches_router.get("/batches", response_model=BatchListResponse)
async def list_batches(
    user: dict = Depends(get_current_user),
    service: BatchService = Depends(get_batch_service),
):
    batches = await service.list_batches(user.get("sub"), limit=BATCH_LIST_LIMIT)
    return BatchListResponse(items=[BatchResponse.model_validate(b) for b in batches])


@batches_router.get("/batches/{batch_id}", response_model=BatchResponse)
async def get_batch(
    batch_id: UUID,
    user: dict = Depends(get_current_user),
    service: BatchService = Depends(get_batch_service),
):
    batch = await service.get_batch(batch_id, user.get("sub"))
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


@batches_router.post("/batches/{batch_id}/cancel", response_model=BatchResponse)
async def cancel_batch(
    batch_id: UUID,
    user: dict = Depends(get_current_user),
    service: BatchService = Depends(get_batch_service),
):
    batch = await service.get_batch(batch_id, user.get("sub"))
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    return await service.request_cancel(batch)


RETRYABLE_TASK_STATUSES = ("failed", "cancelled")


@batches_router.post(
    "/batches/{batch_id}/retry",
    response_model=BatchResponse,
    status_code=201,
    dependencies=[Depends(rate_limit("batch", 30))],
)
async def retry_batch(
    batch_id: UUID,
    background: BackgroundTasks,
    failed_only: bool = False,
    user: dict = Depends(get_current_user),
    service: BatchService = Depends(get_batch_service),
    session: AsyncSession = Depends(get_db_session),
):
    """Run a finished batch again as a new batch: every input, or with
    ``failed_only`` just the inputs whose task failed or was cancelled."""
    batch = await service.get_batch(batch_id, user.get("sub"))
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    if batch.status not in TERMINAL_BATCH_STATUSES:
        raise HTTPException(status_code=409, detail="The batch is still running; cancel it or wait for it to finish")
    tasks = await service.list_tasks(batch_id)
    inputs = [
        t.input_data or ""
        for t in tasks
        if not failed_only or t.status in RETRYABLE_TASK_STATUSES
    ]
    if not inputs:
        raise HTTPException(status_code=400, detail="No failed or cancelled tasks to retry")
    request = BatchCreateRequest(
        code=batch.code,
        inputs=inputs,
        node_ids=[UUID(n) for n in json.loads(batch.node_ids_json or "[]")],
        runtime=batch.runtime,
        custom_command=batch.custom_command,
        custom_filename=batch.custom_filename,
        cpu_per_task=batch.cpu_per_task,
        timeout_seconds=batch.timeout_seconds,
        execution_policy=batch.execution_policy,
        project=ProjectSpec.model_validate_json(batch.project_json) if batch.project_json else None,
    )
    return await _start_batch(request, background, user, service, session)


@batches_router.get("/batches/{batch_id}/tasks", response_model=BatchTasksResponse)
async def get_batch_tasks(
    batch_id: UUID,
    user: dict = Depends(get_current_user),
    service: BatchService = Depends(get_batch_service),
):
    batch = await service.get_batch(batch_id, user.get("sub"))
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found")
    tasks = await service.list_tasks(batch_id)
    return BatchTasksResponse(
        batch=BatchResponse.model_validate(batch),
        tasks=[TaskResponse.model_validate(t) for t in tasks],
    )


# ==============================================================================
# 4. TRAINING ROUTER (/train)
# ==============================================================================

train_router = APIRouter()


_MODEL_FORMATS = {"builtin": "onnx", "custom": "json", "spark": "spark-zip"}
MAX_MODEL_PARAMS_BYTES = 64 * 1024


def _family(model_type: str) -> Optional[str]:
    try:
        return model_family(model_type)
    except ValueError:
        return None


def _train_run_response(run: TrainingRun) -> TrainRunResponse:
    family = _family(run.model_type)
    return TrainRunResponse(
        id=run.id,
        owner_id=run.owner_id,
        dataset_id=run.dataset_id,
        model_type=run.model_type,
        model_family=family,
        engine=getattr(run, "engine", None),
        mode=run.mode,
        rounds=run.rounds,
        status=run.status,
        message=run.message,
        metrics=json.loads(run.metrics_json) if run.metrics_json else None,
        history=json.loads(run.history_json) if run.history_json else None,
        shards=json.loads(run.shards_json) if run.shards_json else None,
        details=json.loads(run.details_json) if getattr(run, "details_json", None) else None,
        model_format=_MODEL_FORMATS.get(family or ""),
        created_at=run.created_at,
    )


@train_router.get("/engines", response_model=EngineListResponse)
async def get_engines(refresh: bool = False, user: dict = Depends(get_current_user)):
    """Which training engines this master can run. Nothing is installed here."""
    items = await asyncio.to_thread(list_engines, refresh)
    return EngineListResponse(items=items)


@train_router.get("/engines/spark_ray/models", response_model=SparkModelListResponse)
async def get_spark_models(refresh: bool = False, user: dict = Depends(get_current_user)):
    """Every Spark MLlib estimator of the installed Spark version (starts Spark)."""
    try:
        await asyncio.to_thread(require_engine, SPARK_RAY)
    except EngineUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    from ml.spark_ml import catalog

    items = await asyncio.to_thread(catalog, refresh)
    return SparkModelListResponse(items=sorted(items.values(), key=lambda m: (m["family"], m["name"])))


@train_router.get("/models/custom/template")
async def get_custom_model_template(user: dict = Depends(get_current_user)):
    """A starter model.py showing the init/train/evaluate contract."""
    return {"filename": "model.py", "code": STARTER_TEMPLATE}


async def _validate_train_request(request: TrainRequest) -> str:
    """Reject requests that cannot run, before anything is stored. Returns the family."""
    settings = get_settings()
    family = model_family(request.model_type)

    def bad(detail: str) -> HTTPException:
        return HTTPException(status_code=400, detail=detail)

    if request.engine == SPARK_RAY:
        try:
            await asyncio.to_thread(require_engine, SPARK_RAY)
        except EngineUnavailable as exc:
            raise bad(str(exc)) from exc
    if len(json.dumps(request.model_params)) + len(json.dumps(request.param_grid)) > MAX_MODEL_PARAMS_BYTES:
        raise bad("model_params and param_grid are too large (64 KB max)")

    if family == "custom":
        if request.mode != "multi":
            raise bad("Custom models train on worker nodes: use mode 'multi'")
        if request.param_grid:
            raise bad("param_grid applies to Spark models only")
        code = request.model_code or ""
        if len(code.encode("utf-8")) > settings.max_code_bytes:
            raise bad(f"model_code is larger than {settings.max_code_bytes} bytes")
        try:
            validate_model_code(code)
        except ValueError as exc:
            raise bad(str(exc)) from exc
    elif request.model_code:
        raise bad("model_code is only used with model_type 'custom'")

    if family == "builtin" and (request.model_params or request.param_grid):
        raise bad("model_params and param_grid are not used by the built-in models")

    if family == "spark":
        if request.engine != SPARK_RAY:
            raise bad("Spark MLlib models need engine 'spark_ray'")
        from ml.spark_ml import model_info, validate_params

        def _check() -> dict:
            validate_params(request.model_type, request.model_params, request.param_grid)
            return model_info(request.model_type)

        try:
            info = await asyncio.to_thread(_check)
        except ValueError as exc:
            raise bad(str(exc)) from exc
        if info["needs_target"] and not request.target_column:
            raise bad(f"{info['name']} needs a target_column")
    elif not request.target_column:
        raise bad("target_column is required")
    return family


@train_router.post(
    "/train",
    response_model=TrainRunResponse,
    status_code=201,
    dependencies=[Depends(rate_limit("train", 20))],
)
async def start_training(
    request: TrainRequest,
    background: BackgroundTasks,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    await _validate_train_request(request)
    hyper = {
        "learning_rate": request.learning_rate,
        "epochs": request.epochs,
        "local_cores": request.local_cores,
        "engine": request.engine,
        "target_column": request.target_column,
        "feature_columns": request.feature_columns,
        "num_classes": request.num_classes,
        "node_ids": [str(n) for n in request.node_ids],
        "execution_policy": request.execution_policy,
        "model_params": request.model_params,
        "param_grid": request.param_grid,
        "test_fraction": request.test_fraction,
        "uses_gpu": request.uses_gpu,
        "seed": request.seed,
    }
    run = TrainingRun(
        owner_id=user.get("sub"),
        dataset_id=request.dataset_id,
        model_type=request.model_type,
        mode=request.mode,
        engine=request.engine,
        rounds=request.rounds,
        hyperparams_json=json.dumps(hyper),
        model_code=request.model_code if request.model_type == "custom" else None,
        status="pending",
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    background.add_task(run_training, run.id)
    return _train_run_response(run)


@train_router.get("/train", response_model=list[TrainRunResponse])
async def list_training_runs(
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    result = await session.execute(
        select(TrainingRun)
        .where(TrainingRun.owner_id == user.get("sub"))
        .order_by(desc(TrainingRun.created_at))
        .limit(20)
    )
    return [_train_run_response(run) for run in result.scalars().all()]


async def _owned_run(session: AsyncSession, run_id: UUID, owner_id: str, *, with_artifact: bool = False):
    stmt = select(TrainingRun).where(TrainingRun.id == run_id, TrainingRun.owner_id == owner_id)
    if with_artifact:
        stmt = stmt.options(undefer(TrainingRun.artifact))
    run = (await session.execute(stmt)).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Training run not found")
    return run


@train_router.get("/train/{run_id}", response_model=TrainRunResponse)
async def get_training_run(
    run_id: UUID,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    return _train_run_response(await _owned_run(session, run_id, user.get("sub")))


@train_router.get("/train/{run_id}/model")
async def download_training_model(
    run_id: UUID,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """ONNX for built-in models, params.json for custom models, and a zipped
    Spark PipelineModel (load with PipelineModel.load) for Spark models."""
    run = await _owned_run(session, run_id, user.get("sub"), with_artifact=True)
    if run.status != "completed":
        raise HTTPException(status_code=409, detail="Model is not available until training completes")
    family = _family(run.model_type)

    if family == "spark":
        if not run.artifact:
            raise HTTPException(status_code=409, detail="This run has no stored Spark model")
        return Response(
            content=run.artifact,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="spark-model-{run.id}.zip"'},
        )
    if not run.weights_json:
        raise HTTPException(status_code=409, detail="This run has no stored model")
    if family == "custom":
        return Response(
            content=run.weights_json,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="params-{run.id}.json"'},
        )
    try:
        model_bytes = build_onnx_model(run.model_type, run.weights_json)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Could not export model: {exc}") from exc
    return Response(
        content=model_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="model-{run.id}.onnx"'},
    )


@train_router.get("/train/{run_id}/data")
async def download_training_data(
    run_id: UUID,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    run = await _owned_run(session, run_id, user.get("sub"))
    hyper = json.loads(run.hyperparams_json or "{}")
    try:
        rows, feature_names = await export_rows(run.dataset_id, run.owner_id, hyper)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not rebuild training data: {exc}")

    payload = build_train_data(
        run, rows, feature_names, hyper,
        metrics=json.loads(run.metrics_json) if run.metrics_json else None,
        history=json.loads(run.history_json) if run.history_json else None,
    )
    return Response(
        content=json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="train_data-{run.id}.json"'},
    )


@train_router.delete("/train/{run_id}", status_code=204)
async def delete_training_run(
    run_id: UUID,
    user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    run = await _owned_run(session, run_id, user.get("sub"))
    await session.delete(run)
    await session.commit()
    return None

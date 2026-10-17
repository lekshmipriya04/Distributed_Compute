from typing import Optional
from uuid import UUID
from fastapi import APIRouter, Depends, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from blob_store import FileTooLarge, build_stores
from schemas import DatasetResponse, DatasetListResponse
from dataset_service import DatasetService
from config import get_storage_settings
from database import get_db_manager
from shared.common.auth import get_current_user
from shared.common.exceptions import ValidationError

router = APIRouter()

async def get_db_session() -> AsyncSession:
    db_manager = get_db_manager()
    async with db_manager.session() as session:
        yield session

def get_dataset_service(session: AsyncSession = Depends(get_db_session)) -> DatasetService:
    settings = get_storage_settings()
    return DatasetService(session, build_stores(settings), settings)


def resolve_owner_id(user: dict, on_behalf_of: Optional[str]) -> str:
    """Pick the owner whose datasets this request may see (API_CONTRACT §5).

    Normally that is the caller (``jwt.sub``). Internal service tokens
    (``create_internal_token``, claim ``internal: true``) may act for a user by
    sending ``X-On-Behalf-Of: <owner_id>``. The header is ignored for every
    other token, so a regular user cannot read someone else's data with it.
    """
    if user.get("internal") is True and on_behalf_of and on_behalf_of.strip():
        return on_behalf_of.strip()
    sub = user.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token has no subject")
    return str(sub)


def get_owner_id(
    user: dict = Depends(get_current_user),
    x_on_behalf_of: Optional[str] = Header(default=None, alias="X-On-Behalf-Of"),
) -> str:
    """FastAPI dependency wrapping :func:`resolve_owner_id`."""
    return resolve_owner_id(user, x_on_behalf_of)


@router.post("/datasets", response_model=DatasetResponse, status_code=201)
async def upload_dataset(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(None),
    owner_id: str = Depends(get_owner_id),
    service: DatasetService = Depends(get_dataset_service)
):
    """Store a new dataset file and register it."""
    dataset = await service.create_dataset(file, name, description, owner_id)
    return dataset

@router.post("/datasets/images", response_model=DatasetResponse, status_code=201)
async def upload_image_dataset(
    file: UploadFile = File(...),
    name: str = Form(...),
    description: str = Form(None),
    image_size: int = Form(8),
    color: str = Form("grayscale"),
    owner_id: str = Depends(get_owner_id),
    service: DatasetService = Depends(get_dataset_service)
):
    """Convert a .zip of label-foldered images into a flattened pixel CSV dataset.

    Layout: ``<label>/<image files>``. Images are grayscaled, resized to a
    square ``image_size`` and flattened to ``p0 … p{S*S-1}`` + numeric ``label``;
    the stored artifact is a normal CSV so training is unchanged.
    """
    limit = get_storage_settings().max_upload_size_bytes
    if file.size is not None and file.size > limit:
        raise FileTooLarge(f"The file is larger than the {limit} byte limit")
    try:
        content = await file.read()
        dataset = await service.create_dataset_from_images(
            content, name, description, owner_id, image_size, color
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return dataset

@router.get("/datasets/{dataset_id}", response_model=DatasetResponse)
async def get_dataset(
    dataset_id: UUID,
    owner_id: str = Depends(get_owner_id),
    service: DatasetService = Depends(get_dataset_service)
):
    dataset = await service.get_dataset(dataset_id, owner_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset

@router.get("/datasets/{dataset_id}/download")
async def download_dataset(
    dataset_id: UUID,
    owner_id: str = Depends(get_owner_id),
    service: DatasetService = Depends(get_dataset_service)
):
    """Stream the raw dataset file.

    Consumed by ssh-executor's training runs (internal token plus
    ``X-On-Behalf-Of: <run owner>``) and by any client that needs the raw
    bytes. Returns the file with a content type matching its stored format.
    The file is streamed, so large datasets never sit in memory.
    """
    try:
        chunks, file_format, size = await service.download_dataset(dataset_id, owner_id)
    except ValidationError:
        raise HTTPException(status_code=404, detail="Dataset not found")

    media_type = "text/csv" if file_format == "csv" else "application/octet-stream"
    headers = {"Content-Disposition": f'attachment; filename="{dataset_id}.{file_format}"'}
    if size is not None:
        headers["Content-Length"] = str(size)
    return StreamingResponse(chunks, media_type=media_type, headers=headers)


@router.get("/datasets", response_model=DatasetListResponse)
async def list_datasets(
    skip: int = 0,
    limit: int = 100,
    owner_id: str = Depends(get_owner_id),
    service: DatasetService = Depends(get_dataset_service)
):
    datasets, total = await service.list_datasets(owner_id, skip, limit)
    return DatasetListResponse(items=datasets, total=total)


@router.delete("/datasets/{dataset_id}", status_code=204)
async def delete_dataset(
    dataset_id: UUID,
    owner_id: str = Depends(get_owner_id),
    service: DatasetService = Depends(get_dataset_service)
):
    """Remove a dataset from the list (DB row + best-effort file removal)."""
    deleted = await service.delete_dataset(dataset_id, owner_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return None

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from blob_store import LOCAL, build_stores
from routes import router as dataset_router
from config import get_storage_settings
from database import get_db_manager
from shared.common.error_handlers import generic_exception_handler, platform_exception_handler
from shared.common.exceptions import PlatformException
from shared.common.logging_config import configure_logging
from shared.common.middleware import LoggingMiddleware, MetricsMiddleware, RequestIDMiddleware
from shared.common.security import assert_secure_database_url, assert_secure_secrets

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    settings = get_storage_settings()
    configure_logging(level=settings.log_level, service_name=settings.service_name, environment=settings.environment.value)
    assert_secure_secrets(settings.environment, JWT_SECRET=settings.jwt_secret)
    assert_secure_database_url(settings.environment, settings.database_url)

    stores = build_stores(settings)
    if settings.storage_backend == LOCAL:
        # Fail fast on a missing or read-only data directory.
        stores[LOCAL].prepare()

    db_manager = get_db_manager()
    await db_manager.initialize()
    await db_manager.create_tables()
    logger.info("storage_service_started", extra={"storage_backend": settings.storage_backend})
    yield
    # Shutdown
    await db_manager.dispose()
    logger.info("storage_service_stopped")


def create_app() -> FastAPI:
    settings = get_storage_settings()
    
    app = FastAPI(
        title="Storage Service",
        version=settings.service_version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(LoggingMiddleware, service_name=settings.service_name)
    app.add_middleware(MetricsMiddleware, service_name=settings.service_name)

    # Exception Handlers
    app.add_exception_handler(PlatformException, platform_exception_handler)
    app.add_exception_handler(Exception, generic_exception_handler)

    # Routers
    app.include_router(dataset_router, prefix="/api/v1/storage", tags=["datasets"])

    # Metrics
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    @app.get("/health")
    async def health_check():
        """Healthy when the store new datasets go to can be used."""
        store = build_stores(settings)[settings.storage_backend]
        ready = await store.ready()
        body = {
            "status": "ok" if ready else "degraded",
            "service": settings.service_name,
            "storage": {"backend": settings.storage_backend, "ready": ready},
        }
        if settings.storage_backend == LOCAL and ready:
            body["storage"]["free_bytes"] = store.free_bytes()
        return JSONResponse(body, status_code=200 if ready else 503)

    return app

app = create_app()

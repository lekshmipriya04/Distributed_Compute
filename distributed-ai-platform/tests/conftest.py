import importlib
import os
import sys
from pathlib import Path

import pytest

# BUG-14 fix: force NullPool in tests to avoid asyncpg connection leaks
os.environ["USE_NULL_POOL"] = "true"

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"

# Each microservice runs with its own directory as the working dir and the repo
# root on PYTHONPATH, so its modules use flat imports (`from models import ...`,
# `from config import ...`). The service directories are hyphenated, so they are
# NOT importable as normal Python packages, and several services share module
# names (config, models, database, schemas). To load a specific service's module
# in-process we (1) purge any cached same-named service-local modules, then
# (2) put that one service dir at the front of sys.path and import fresh.

# Ensure the repo root is importable so `shared.*` resolves.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Module names that live at every service root and therefore collide in
# sys.modules if a previous test imported a different service's copy.
_SERVICE_LOCAL_NAMES = {
    "config",
    "models",
    "database",
    "schemas",
    "routes",
    "main",
    "error_handlers",
    "dataset_service",
    "blob_store",
    "migrate_datasets",
}


def import_service_module(service_dirname: str, module_name: str):
    """Import ``module_name`` from ``services/<service_dirname>/`` in isolation.

    Clears cached service-local modules first so importing one service after
    another does not resolve a sibling service's same-named module.
    """
    service_path = SERVICES_DIR / service_dirname
    if not service_path.is_dir():
        raise FileNotFoundError(f"Service directory not found: {service_path}")

    # Already loaded from this service: reuse it. Reloading would define the
    # service's ORM tables a second time on the shared SQLAlchemy metadata.
    cached = sys.modules.get(module_name)
    if cached is not None and str(service_path) in (getattr(cached, "__file__", "") or ""):
        return cached
    # Loading a module the service shares with others ("models", "config", …)
    # from a different service: clear them first, unless this service's own
    # copies are what is loaded (then extend them, as an import would).
    loaded_here = any(
        str(service_path) in (getattr(sys.modules.get(n), "__file__", "") or "")
        for n in _SERVICE_LOCAL_NAMES
    )
    if not loaded_here:
        for name in list(sys.modules):
            if name in _SERVICE_LOCAL_NAMES:
                del sys.modules[name]

    sys.path.insert(0, str(service_path))
    try:
        return importlib.import_module(module_name)
    finally:
        # Leave the service dir on sys.path only for the duration of the import;
        # the imported module object keeps working, but we avoid polluting the
        # path for the next service's import.
        try:
            sys.path.remove(str(service_path))
        except ValueError:
            pass


@pytest.fixture
def mock_settings():
    return {}

.PHONY: init up up-dev up-hdfs migrate-datasets backup down reset clean build enable-spark-ray disable-spark-ray test test-unit lint lint-full format frontend-build go-test

init:
	pip install -e .[dev]

# config/.env is created from the example on first use (local defaults).
config/.env:
	cp config/.env.example config/.env

up: config/.env
	docker compose up -d --build

# Also starts the two throwaway SSH provider containers (profile "dev").
up-dev: config/.env
	docker compose --profile dev up -d --build

# Optional: store new datasets in HDFS instead of local files (starts the
# NameNode and three DataNodes too; needs about 1.4 GB more memory).
up-hdfs: config/.env
	STORAGE_BACKEND=hdfs docker compose --profile hdfs up -d --build

# Moves datasets stored in HDFS (before 2.0) to local storage: starts HDFS,
# copies each file and switches its row, then stops HDFS again. Safe to re-run.
migrate-datasets: config/.env
	docker compose --profile hdfs up -d --wait namenode datanode-1 datanode-2 datanode-3
	docker compose --profile hdfs up -d --build --wait storage-service
	docker compose exec storage-service python -m migrate_datasets
	docker compose --profile hdfs stop namenode datanode-1 datanode-2 datanode-3 hdfs-init
	@echo "Done. Once you have checked your datasets, the old HDFS volumes can go:"
	@echo "  docker volume rm $$(docker compose --profile hdfs volumes --format '{{.Name}}' | grep hadoop_ | tr '\n' ' ')"

# Copies the database and the dataset files to backups/<date>/.
backup:
	@mkdir -p backups/$$(date +%Y%m%d-%H%M%S) && d=backups/$$(ls -1 backups | tail -1) && \
	docker compose exec -T postgres sh -c 'pg_dump -U "$$POSTGRES_USER" "$$POSTGRES_DB"' > $$d/database.sql && \
	docker compose exec -T storage-service tar czf - -C /data . > $$d/datasets.tar.gz && \
	echo "Backed up to $$d"

# Stops everything and keeps your data (database, datasets, registered nodes).
down:
	docker compose --profile dev --profile hdfs down

# Stops everything and DELETES the database, dataset and HDFS volumes.
reset:
	docker compose --profile dev --profile hdfs down -v

# Removes caches and build output. Leaves node_modules and your data alone.
clean:
	find . -name __pycache__ -type d -prune -not -path "*/node_modules/*" -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache .pytest_cache .coverage coverage.xml htmlcov *.egg-info
	rm -rf frontend/dist distributed-compute-vscode/dist distributed-compute-vscode/*.vsix
	find . -name .DS_Store -not -path "*/node_modules/*" -delete

build:
	docker compose build

# Optional Spark + Ray engine, on the master only (adds about 1 GB). The choice
# is kept in .env, which docker compose reads on every later `make up`.
enable-spark-ray:
	@touch .env && grep -v '^WITH_SPARK_RAY=' .env > .env.tmp; echo 'WITH_SPARK_RAY=true' >> .env.tmp && mv .env.tmp .env
	docker compose up -d --build ssh-executor
	@echo "Spark + Ray is enabled on the master. Worker nodes are unchanged."

disable-spark-ray:
	@touch .env && grep -v '^WITH_SPARK_RAY=' .env > .env.tmp; echo 'WITH_SPARK_RAY=false' >> .env.tmp && mv .env.tmp .env
	docker compose up -d --build ssh-executor
	@echo "Spark + Ray is disabled; training uses pure Python."

test:
	pytest tests/unit/ -q

test-unit:
	pytest tests/unit/ -v --cov=services --cov=shared --cov-report=xml

# Gate: syntax errors and pyflakes findings (unused/undefined names), plus a
# non-strict mypy pass over the shared library. `lint-full` runs the complete
# ruff rule set from pyproject.toml, which the code base does not pass yet.
lint:
	ruff check --select E9,F services/ shared/ tests/
	mypy shared/

lint-full:
	ruff check services/ shared/ tests/

format:
	ruff format services/ shared/ tests/

frontend-build:
	cd frontend && npm ci && npm run build

go-test:
	cd services/extension-gateway && go test ./...

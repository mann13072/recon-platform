.PHONY: install dev test lint typecheck migrate seed benchmark checklist fmt down

install:
	python -m pip install -e ".[dev]"
	cd apps/web && npm install

dev:
	docker compose up

test:
	pytest -q
	cd apps/web && npm test --if-present

lint:
	ruff check .
	cd apps/web && npm run lint --if-present

typecheck:
	mypy apps packages

fmt:
	ruff format .
	ruff check --fix .

migrate:
	alembic upgrade head

seed:
	python scripts/seed_demo.py

benchmark:
	python scripts/benchmark_matching.py

checklist:
	python scripts/verify_build_checklist.py

down:
	docker compose down -v

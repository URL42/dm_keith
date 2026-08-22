.PHONY: setup test cov lint fmt typecheck check run docker

setup:          ## install dependencies into .venv
	uv sync --extra dev

test:
	uv run pytest

cov:            ## tests with a coverage report
	uv run pytest --cov=src --cov-report=term-missing

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

typecheck:
	uv run mypy

check: lint typecheck test   ## everything CI would run

run:
	uv run python -m src.main

docker:
	docker compose up --build -d

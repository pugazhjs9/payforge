# PayForge

A multi-merchant payment gateway API built with FastAPI and MySQL.

> Work in progress.

## Run it locally

Requirements: Python 3.13 + [uv](https://docs.astral.sh/uv/), Docker.

```sh
uv sync
cp .env.example .env
docker compose up -d mysql
uv run uvicorn payforge.main:app --reload
```

## Checks

```sh
uv run ruff check . && uv run mypy apps/api/src && uv run pytest
```

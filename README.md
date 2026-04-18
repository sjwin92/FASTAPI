# Supermarket Price Tracker API

Production-ready FastAPI backend scaffold for supermarket price tracking.

## Stack

- FastAPI
- PostgreSQL + SQLAlchemy ORM
- Alembic migrations
- Docker + docker-compose

## Project structure

```text
app/
  main.py
  db.py
  models.py
  schemas.py
  adapters/
  services/
```

## Endpoints

- `GET /health`
- `GET /retailers`
- `GET /search`
- `POST /track`
- `GET /history/{id}`

> Note: scraping is intentionally not implemented yet. Retailer adapters currently expose metadata only.

## Local run (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
export DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/price_tracker'
alembic upgrade head
uvicorn app.main:app --reload
```

## Docker run

```bash
cp .env.example .env
docker compose up --build
```

The API container runs migrations on start (`alembic upgrade head`) and then starts Uvicorn.

## Migrations

Create new migration:

```bash
alembic revision --autogenerate -m "describe change"
```

Apply migrations:

```bash
alembic upgrade head
```

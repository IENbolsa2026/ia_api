#!/usr/bin/make

# Atajos opcionales para no recordar los comandos de desarrollo.
# Ejecutar cada servicio en una terminal distinta: Redis, API y worker.

help:
	@echo "install       Instala dependencias en .venv"
	@echo "run-api       Inicia FastAPI en modo desarrollo"
	@echo "run-worker    Inicia el worker de Celery"
	@echo "run-redis     Inicia Redis mediante Docker Compose"
	@echo "stop-redis    Detiene Redis"

install:
	.venv/bin/python -m pip install -r requirements.txt

run-api:
	.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload

run-worker:
	.venv/bin/celery -A app.core.celery_app.celery_app worker --loglevel=info --pool=solo

run-redis:
	docker compose up -d redis

stop-redis:
	docker compose down

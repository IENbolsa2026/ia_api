#!/usr/bin/make

.PHONY: help install run lint lint-fix

help:
	@echo "make help      show this menu"
	@echo "make install   crear .venv e instalar requirements.txt"
	@echo "make run       levantar uvicorn app.main:app --reload sobre 0.0.0.0:8000"
	@echo "make lint      ruff check + formato"
	@echo "make lint-fix  ruff check + fixer"

install:
	python -m venv .venv && .venv/bin/pip install -r requirements.txt

run:
	.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

lint:
	.venv/bin/ruff check app

lint-fix:
	.venv/bin/ruff check app --fix
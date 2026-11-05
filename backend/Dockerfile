FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && groupadd --gid 10001 ia \
    && useradd --uid 10001 --gid ia --no-create-home ia \
    && install -d -m 0700 -o ia -g ia /app/private_cv_artifacts

COPY app ./app
COPY ien_contracts ./ien_contracts

USER ia
EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]

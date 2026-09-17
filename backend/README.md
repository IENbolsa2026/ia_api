# Microservicio de analisis de CVs

Este proyecto recibe el texto de un CV, lo analiza con Groq en segundo plano y
envia el resultado JSON a otra aplicacion mediante un callback HTTP.

## Que ocurre cuando llega un CV

```text
Aplicacion origen
   | POST /api/v1/cv-analyses { candidate_id, cv_text }
   v
FastAPI valida el JSON y publica una tarea en Redis
   | responde 202 { job_id, status: "queued" }
   v
Worker Celery toma la tarea y consulta Groq
   | POST resultado final al callback configurado
   v
Aplicacion origen guarda o muestra el resultado
```

FastAPI atiende HTTP; no deberia esperar los segundos que tarda la IA. Celery
ejecuta esa parte lenta en un proceso worker independiente. Redis es la cola que
conecta ambos procesos y tambien guarda temporalmente el estado de cada tarea.

## Estructura

```text
app/
  main.py                         Crea la aplicacion FastAPI y monta rutas.
  api/v1/endpoints/cv_analyses.py Recibe pedidos HTTP y consulta estado.
  schemas/cv_analysis.py          Define y valida los cuerpos JSON.
  core/config.py                  Lee variables del archivo .env.
  core/celery_app.py              Configura la cola Celery/Redis.
  tasks/cv_analysis_tasks.py      Ejecuta el analisis fuera de FastAPI.
  services/cv_pipeline.py         Logica de IA con Groq, sin HTTP ni Celery.
  services/result_notifier.py     Hace POST del resultado al otro sistema.
```

Los archivos antiguos `services/job_store.py` y `services/cv_analysis_runner.py`
ya no participan del flujo. Se dejaron como referencia del enfoque anterior de
tareas en memoria; pueden eliminarse cuando confirmes que Celery funciona.

## Primera ejecucion

Necesitas Python 3.10 o 3.11 y Docker (solo para ejecutar Redis de forma
sencilla). El entorno virtual local es `backend/.venv`; ya fue creado para este
proyecto y esta ignorado por Git.

1. Desde `backend`, crea el entorno virtual si todavia no existe e instala las
   dependencias:

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ```

   Para ejecutar comandos manualmente puedes activarlo con
   `source .venv/bin/activate`. Los comandos de este README y del `Makefile` no
   requieren activarlo porque usan rutas explicitas a `.venv/bin/...`.

2. Crea tu configuracion local:

   ```bash
   cp .env.example .env
   ```

   Edita `.env`. Como minimo coloca una clave real en `GROQ_API_KEY` y la URL
   real de la ruta receptora en `CV_RESULT_CALLBACK_URL`.

3. En una terminal, inicia Redis:

   ```bash
   docker compose up -d redis
   ```

4. En otra terminal, inicia FastAPI:

   ```bash
   .venv/bin/uvicorn app.main:app --reload
   ```

5. En una tercera terminal, inicia el worker:

   ```bash
   .venv/bin/celery -A app.core.celery_app.celery_app worker --loglevel=info
   ```

   Los atajos equivalentes son `make run-redis`, `make run-api` y
   `make run-worker`.

6. Abre `http://127.0.0.1:8000/docs`. FastAPI genera esta interfaz y la
   documentacion OpenAPI automaticamente a partir de las rutas y modelos.

## Contrato HTTP de entrada

`POST /api/v1/cv-analyses`

```json
{
  "candidate_id": 123,
  "cv_text": "Ana Perez. Desarrolladora Python con 3 anos de experiencia..."
}
```

Respuesta inmediata (`202 Accepted`):

```json
{
  "job_id": "id-generado-por-celery",
  "status": "queued"
}
```

Validaciones automaticas de FastAPI/Pydantic: `candidate_id` debe ser mayor a
cero y `cv_text` no puede estar vacio. Si el JSON no las cumple, FastAPI responde
`422` explicando el campo invalido.

## Consultar estado

`GET /api/v1/cv-analyses/{job_id}` devuelve uno de estos estados:

| Estado de la API | Significado |
| --- | --- |
| `queued` | El mensaje espera un worker. |
| `processing` | Un worker esta analizando el CV o reintentando. |
| `completed` | El resultado se proceso y el callback respondio correctamente. |
| `failed` | Se agotaron los reintentos. Revisar logs del worker. |

El `GET` es util para pantallas de seguimiento. La integracion principal es el
callback: la aplicacion origen no necesita hacer polling si puede recibirlo.

## Contrato del callback

Al completar exitosamente, el worker envia un `POST` a
`CV_RESULT_CALLBACK_URL` con este cuerpo:

```json
{
  "job_id": "id-generado-por-celery",
  "candidate_id": 123,
  "status": "completed",
  "result": {
    "resumen_ejecutivo": "...",
    "habilidades_analizadas": []
  },
  "error": null
}
```

Al fallar despues de todos los reintentos, envia el mismo contrato con
`status: "failed"`, `result: null` y un mensaje en `error`. Si configuraste
`CV_RESULT_CALLBACK_SECRET`, tambien se envia el header:

```text
X-Callback-Secret: tu-secreto-compartido
```

La otra aplicacion debe verificar ese valor antes de aceptar el callback y debe
guardar `job_id` para tratar mensajes repetidos de forma idempotente. Un callback
puede repetirse por reintentos aunque el primer POST haya llegado pero su
respuesta se haya perdido.

## Responsabilidad de cada tecnologia

- **FastAPI**: define rutas con decoradores (`@router.post`), valida JSON con
  modelos Pydantic y responde HTTP.
- **Pydantic**: transforma y verifica los datos entrantes antes de que la
  funcion de la ruta los use.
- **Celery**: serializa una tarea y la ejecuta en procesos worker escalables.
- **Redis**: transporta la tarea entre FastAPI y el worker y almacena estados.
- **Groq**: realiza la extraccion y consolidacion con el modelo configurado.
- **httpx**: hace la solicitud HTTP saliente al sistema receptor.

## Desarrollo y problemas habituales

- Si `POST /cv-analyses` falla al instante, comproba que Redis esta activo y que
  `CELERY_BROKER_URL` apunta al host correcto.
- Si una tarea queda en `queued`, comproba que el worker esta iniciado y muestra
  `app.tasks.analyze_cv` en sus tareas registradas.
- Si queda en `failed`, revisa los logs del worker: puede ser una clave de Groq,
  el modelo, conectividad o una respuesta no exitosa del callback.
- Dentro de Docker, `localhost` significa el contenedor actual. Si FastAPI,
  worker y la aplicacion receptora viven en contenedores separados, usa el nombre
  del servicio Docker correspondiente en las URLs.
- No uses `BackgroundTasks` para este trabajo: corre dentro del mismo proceso
  web, se pierde al reiniciar y no ofrece una cola durable ni reintentos.

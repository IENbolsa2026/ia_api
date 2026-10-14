# Microservicio de analisis de CVs

Este proyecto recibe un PDF, extrae su texto y lo analiza con Groq en segundo plano; luego
envia el resultado JSON a otra aplicacion mediante un callback HTTP.

## Que ocurre cuando llega un CV

```text
Aplicacion origen
   | POST /api/v1/cv-analyses (PDF + IDs, multipart/form-data)
   v
FastAPI valida el PDF y publica una tarea en Redis
   | responde 202 { job_id, status: "queued" }
   v
Worker Celery toma la tarea y consulta Groq
   | POST resultado final al callback configurado
   | tras confirmar el guardado, publica cv.analysis.complete en Redis
   v
Aplicacion origen guarda el resultado y avisa por WebSocket al postulante
```

FastAPI atiende HTTP; no deberia esperar los segundos que tarda la IA. Celery
ejecuta esa parte lenta en un proceso worker independiente. Redis es la cola que
conecta ambos procesos y tambien guarda temporalmente el estado de cada tarea.

## Estructura

```text
app/
  main.py                         Crea la aplicacion FastAPI y monta rutas.
  api/v1/endpoints/cv_analyses.py Recibe pedidos HTTP y consulta estado.
  schemas/cv_analysis.py          Define respuestas y estados de la API.
  core/config.py                  Lee variables del archivo .env.
  core/celery_app.py              Configura la cola Celery/Redis.
  tasks/cv_analysis_tasks.py      Ejecuta el analisis fuera de FastAPI.
  services/cv_pipeline.py         Logica de IA con Groq, sin HTTP ni Celery.
  services/result_notifier.py     Hace POST del resultado al otro sistema.
```

Los archivos antiguos `services/job_store.py` y `services/cv_analysis_runner.py`
ya no participan del flujo. Se dejaron como referencia del enfoque anterior de
tareas en memoria; pueden eliminarse cuando confirmes que Celery funciona.

## Ejecutar la pila completa en local

La integración necesita este microservicio, la aplicación Django de
`repositorio/BolsaTrabajo`, Redis y un worker de Celery. Los pasos siguientes
parten de clones nuevos de ambos repositorios. Requieren Python 3.12, Docker y
Docker Compose.

### 1. Preparar este microservicio

Desde `ia_api/backend`, crea el entorno virtual e instala las dependencias:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Para ejecutar comandos manualmente puedes activarlo con
`source .venv/bin/activate`. Los comandos de este README y del `Makefile` no
requieren activarlo porque usan rutas explicitas a `.venv/bin/...`.

### 2. Configurar las variables de entorno

Crea la configuración local:

```bash
cp .env.example .env
```

Edita `.env`. Para la integración local con Django usa, como mínimo:

```dotenv
GROQ_API_KEY=tu-clave-de-groq
CV_MODEL=qwen/qwen3.8-27b
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/1
CHANNEL_REDIS_URL=redis://localhost:6379/2
CV_RESULT_CALLBACK_URL=http://127.0.0.1:8000/ia/llegue/
CV_RESULT_CALLBACK_SECRET=elegi-un-secreto-local
```

La clave `CV_RESULT_CALLBACK_SECRET` debe ser idéntica a
`IA_CALLBACK_TOKEN` en el `.env` de Django. El modelo indicado está disponible
para la cuenta de Groq utilizada por este proyecto.

### 3. Preparar Django

En `repositorio/BolsaTrabajo`, crea su entorno, instala dependencias, configura
el `.env` y ejecuta migraciones. Los valores necesarios son:

```dotenv
IA_API_URL=http://127.0.0.1:8001/api/v1/cv-analyses
IA_CALLBACK_TOKEN=elegi-un-secreto-local
CHANNEL_REDIS_URL=redis://localhost:6379/2
```

Consulta el [README de Django](../../repositorio/BolsaTrabajo/README.md)
para los comandos completos.

### 4. Iniciar los cuatro servicios

En terminales separadas:

```bash
# Terminal 1: cola Redis
docker compose up -d redis
```

```bash
# Terminal 2: worker que procesa PDFs y llama a Groq
make run-worker
```

```bash
# Terminal 3: API que recibe y encola PDFs
make run-api
```

```bash
# Terminal 4, desde repositorio/BolsaTrabajo: aplicación web y callback
.venv/bin/python manage.py runserver 0.0.0.0:8000
```

Los atajos equivalentes son `make run-redis`, `make run-api` y
`make run-worker`.

Abre `http://127.0.0.1:8001/docs` para la documentación de FastAPI y
`http://127.0.0.1:8000/` para Django.

### 5. Verificar la instalación

```bash
curl http://127.0.0.1:8001/
curl -I http://127.0.0.1:8000/
```

FastAPI debe responder `{"message":"CV analyzer API is running"}`; Django,
un `200`. El worker debe mostrar `celery@... ready.`.

## Contrato HTTP de entrada

`POST /api/v1/cv-analyses` recibe un cuerpo `multipart/form-data`:

| Campo | Tipo | Descripción |
| --- | --- | --- |
| `analysis_id` | entero | ID de la solicitud creada por Django. |
| `candidate_id` | entero | ID del postulante. |
| `cv` | archivo PDF | CV de hasta 5 MB. |

FastAPI devuelve la respuesta antes de extraer el texto. El worker de Celery
extrae el PDF y realiza el análisis con Groq en segundo plano.

Respuesta inmediata (`202 Accepted`):

```json
{
  "job_id": "id-generado-por-celery",
  "status": "queued"
}
```

`analysis_id` y `candidate_id` deben ser mayores a cero; el archivo debe ser un
PDF no vacío de hasta 5 MB. Si no se cumplen estas condiciones, FastAPI responde
con un error `4xx` explicando el campo inválido.

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
  "analysis_id": 456,
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
X-IA-Callback-Token: tu-secreto-compartido
```

La otra aplicacion debe verificar ese valor antes de aceptar el callback y debe
guardar `job_id` para tratar mensajes repetidos de forma idempotente. Un callback
puede repetirse por reintentos aunque el primer POST haya llegado pero su
respuesta se haya perdido.

## Responsabilidad de cada tecnologia

- **FastAPI**: define rutas con decoradores (`@router.post`), valida cargas
  multipart y responde HTTP.
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

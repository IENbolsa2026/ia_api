# pdf_analysis_service

Microservicio FastAPI que recibe un PDF vía HTTP, extrae el texto, lo analiza con **Groq** y avisa a **Django** por webhook. El progreso y el resultado se notifican en tiempo real por websocket.

> ¿Querés **aprender FastAPI leyendo este código**? Abrí [doc/guia-flujo.md](doc/guia-flujo.md): tiene el diagrama del flujo de datos, el orden de lectura recomendado y cómo seguir cada etapa en vivo con los `print` de la consola marcados como `[FLUJO]`.

## Estructura

```
app/
├── main.py                  arma la app e incluye los routers
├── config.py                settings (env vars) tipados con Pydantic
├── core/
│   └── connection_manager.py  websockets + registro de jobs en memoria
├── routers/
│   ├── pdf.py               POST /procesar-pdf
│   └── websocket.py         WS  /ws/{job_id}
├── schemas/
│   └── analisis.py          contrato JSON fijo (respuesta, WS, webhook)
├── services/                lógica pura, sin FastAPI adentro
│   ├── pdf_service.py       extraer_texto / resumir_texto
│   ├── groq_service.py      skill + prompt + llamado a Groq
│   └── django_client.py     aviso a Django (webhook)
└── tasks/
    └── procesar_pdf.py      orquesta: extraer → analizar → avisar
```

Cada capa solo conoce a la de abajo: `routers` → `tasks` → `services`. Los `services` no saben nada de HTTP ni de FastAPI, así se testean con un script suelto.

## Puesta en marcha

Desde la raíz del proyecto (donde está `app/`, import path `app.main:app`):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # y completá GROQ_API_KEY (+ DJANGO_WEBHOOK_URL si querés)

uvicorn app.main:app --reload
```

- Docs interactivos: http://localhost:8000/docs
- Health: http://localhost:8000/health

## Probar los servicios solos (sin levantar el server)

```bash
python -c "from app.services.pdf_service import extraer_texto; print(extraer_texto(open('test.pdf','rb').read()))"
```

```bash
python -c "from app.services.groq_service import analizar_texto; print(analizar_texto('Hola, esto es un documento de prueba.'))"
```

## Endpoints

| Método | Ruta            | Descripción |
|--------|-----------------|-------------|
| POST   | `/procesar-pdf` | Multipart `file` (+) `job_id` opcional. Crea el job, dispara la tarea y responde `202` con `{job_id, estado}`. |
| WS     | `/ws/{job_id}`  | Recibe snapshots y `{progreso, resultado, error}`. |
| GET    | `/health`       | Health check. |

### Flujo WS

El server envía mensajes `{tipo: "progreso"|"resultado"|"error"|"snapshot", job_id, estado, progreso, resultado?, error?, mensaje?}`.
Al conectar, manda un `snapshot` con el estado actual; enviar `ping` por el socket devuelve otro snapshot.

## Configuración (`.env`)

| Variable             | Uso |
|----------------------|-----|
| `GROQ_API_KEY`       | Key de Groq (https://console.groq.com/keys) |
| `GROQ_MODEL`         | Modelo, por defecto `llama-3.3-70b-versatile` |
| `DJANGO_WEBHOOK_URL` | URL del webhook de Django. Vacía ⇒ se omite el aviso. |
| `PDF_MAX_MB`         | Límite de subida (20 MB) |
| `PDF_MAX_CHARACTERES`| Recorte del texto antes de mandarlo al modelo |

## Notas

- El registro de jobs es **en memoria** (`app/core/connection_manager.py`): los estados y resultados viven en el proceso. Si se reinicia, los jobs mueren; la fuente de verdad persistente es Django.
- Un PDF escaneado (sin capa de texto) falla en `extraer_texto` con `PdfError` → el job pasa a `ERROR` y se notifica.
- CORS abierto por defecto; ajustar `BACKEND_CORS_ORIGINS`.
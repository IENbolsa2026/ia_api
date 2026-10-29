# Microservicio de analisis de CVs

## Formatos de carga y límites

`POST /api/v1/cv-analyses` acepta el campo multipart `cv` con PDF, DOCX, ODT o
TXT (UTF-8/Windows-1252), hasta 5 MiB. DOC/RTF solo están habilitados cuando
Docker y una imagen conversora aislada están configurados. Consulta
`GET /api/v1/cv-analyses/capabilities` antes de mostrar formatos al usuario.
La detección valida firma y estructura; no confía en el MIME enviado por el
cliente. El límite actual es 20 páginas PDF, 50 MiB descomprimidos para Office
y 100.000 caracteres de texto. No se trunca contenido. El OCR queda fuera de
alcance.

El análisis del CV usa `cv-extraction.v2-packets-soft.v13-education` y mantiene el perfil ampliado
`candidate-profile.v2`. Divide el texto en paquetes de hasta
`CV_PACKET_CHARACTERS` (500 por defecto, limitado por `CV_BLOQUE_CARACTERES`).
Cada llamada utiliza `CV_EXTRACTION_MAX_TOKENS` (950 por defecto). Si una salida
se trunca, solo ese paquete se divide, preservando todo el texto.

El manifiesto y los avances viven dentro del trabajo Redis identificado por su
UUID. La identidad incluye análisis, postulante, versión del CV, hash del archivo,
hash del texto, prompt, proveedor/modelo y configuración. Cada paquete tiene su
propio identificador, rango y hash; un checkpoint de otro trabajo se rechaza.
Los resultados se validan contra el subtexto de su paquete antes de guardarlos.
Los paquetes terminados se reutilizan al reanudar, sin enviarlos otra vez.

La consolidación con IA devuelve grupos de identificadores; el servidor conserva
las evidencias ya extraídas. Solo se publica el perfil cuando están completas
todas las hojas y su cobertura corresponde al texto íntegro del mismo CV.
Si un grupo propuesto combina hechos incompatibles, se conservan como hechos
individuales después de validar que todos los identificadores estén presentes
exactamente una vez. El plan efectivo final queda guardado y validado para
evitar repetirlo tras una interrupción. Las escrituras requieren el propietario vigente del bloqueo.
El limitador de cuota es global; los datos y resultados de los CV son por trabajo.

La versión v8 conserva las diez categorías blandas y también recupera
tecnologías o conocimientos explícitos dentro de un cargo (por ejemplo,
«programador en Python»), de un stack o de una práctica descrita durante
un curso. Un título de curso sin práctica ni conocimiento declarado no basta
para crear una habilidad. Los estudios conservan
`completion_status` (`completed`, `in_progress`, `incomplete`, `unknown`) y citas
literales; no se deduce graduación de fechas ni de mencionar un programa. Estos
campos son aditivos a `completed`, pero requieren actualizar ambas copias del
contrato compartido (API y Django). El cambio no reescribe perfiles históricos:
se debe solicitar un nuevo análisis para aplicar la nueva versión.

Los paquetes iniciales se cortan en límites de palabra cuando no hay salto de
línea ni fin de oración próximo; se evita dividir nombres como «Python» entre
dos paquetes. La consolidación no une tecnologías distintas por un identificador
de concepto genérico. La evaluación con CV sintéticos y sus límites está en
`docs/EXTRACCION_CV_V8.md`.

### Contexto RAG de habilidades

La versión v9 carga instrucciones independientes desde `app/prompts/skills/`:
criterio general, formato compacto de perfil, taxonomía blanda y guías de
perfil técnico o comercial cuando el texto lo justifica. Busca conceptos
relevantes en `app/data/skill_catalog.v1.json` y, si está configurado, en un
PostgreSQL aislado con pgvector. El catálogo define conceptos y contraejemplos;
el modelo recibe el texto original del CV y debe citarlo para declarar cada
habilidad. El catálogo no guarda CV, embeddings de CV ni identidades.

Para habilitar la búsqueda híbrida local, definir `SKILL_CATALOG_DB_PASSWORD`
y `SKILL_CATALOG_DATABASE_URL` en el `.env` privado. En esta instalación local
el DSN apunta a `postgresql://skill_catalog:<contraseña>@127.0.0.1:5434/ien_skill_catalog`.
Luego, desde `backend`, ejecutar:

```powershell
docker compose -f docker-compose.yml -f docker-compose.rag.yml up -d skill_catalog_postgres
.venv\Scripts\python.exe -m tools.import_skill_catalog app\data\skill_catalog.v1.json --initialize --embed
```

`--embed` descarga una vez el modelo multilingüe liviano y genera vectores
localmente; las consultas normales usan la caché local sin descargar. La
importación es explícita e idempotente. Un catálogo propio JSON/CSV puede
añadirse con el mismo comando sin `--initialize`; `--replace` elimina primero
los conceptos anteriores y requiere una decisión consciente. La búsqueda
combina nombres exactos, texto y similitud vectorial; si la base o el modelo
no están disponibles, continúa con el catálogo local. `CV_RAG_CONTEXT_MAX_CHARACTERS`
acota el contexto adicional por llamada. En Docker, si también se ejecuta el
worker en la red de Compose, el host del DSN debe ser
`skill_catalog_postgres:5432`.

Las claves abreviadas se usan solo en el transporte con el modelo. El formato
almacenado por Django y el que recibe el match conserva su contrato. Las llamadas
de ofertas, normalización y match mantienen sus presupuestos actuales.
Las pausas por cuota pueden demorar varios minutos. Una salida incompleta nunca
se presenta como un perfil completo. Los logs solo incluyen etapas, IDs y
cantidades; no registran texto ni citas del CV.

### Indisponibilidad temporal de Gemini

HTTP 500, 502, 503, 504 y fallas de conexión o timeout se tratan como errores
temporales del proveedor. Se comparte una pausa en Redis por clave/modelo, de
30 segundos por defecto, respetando una espera mayor indicada por `Retry-After`
o `RetryInfo`. No hay reintentos HTTP internos ni se devuelven las reservas de
cuota de una solicitud fallida: cada nuevo intento vuelve a pasar por RPM/TPM/RPD.

Celery reencola el trabajo con esperas de 30, 60, 120 y 240 segundos, más hasta
5 segundos de variación, y libera sus bloqueos entre intentos. Una indicación
mayor del proveedor tiene prioridad. Los paquetes ya terminados se conservan;
si el CV avanza, se reinicia su contador de fallos temporales sin progreso.
Después de cinco fallos sin avance se informa que Gemini no está disponible,
en lugar de atribuir el error al archivo. La normalización, extracción de
ofertas y el match también tienen reintentos acotados para estas fallas.

Un callback con estado de entrega `delivered` solo confirma que la aplicación
origen recibió el resultado: este puede ser un éxito o un fallo. El estado
del análisis se obtiene de su resultado y error, no del éxito HTTP del callback.

La conversión opcional de DOC/RTF se instala desde `backend` con:

```powershell
docker build -f docker/cv-converter.Dockerfile -t ien-cv-converter:local .
```

Configura `CV_LEGACY_CONVERTER_IMAGE=ien-cv-converter:local`. El runner usa
contenedor sin red, usuario no privilegiado, filesystem de solo lectura,
límites de memoria/CPU/procesos y timeout. Si no se configura, la API no anuncia
ni acepta esos formatos.

Para desplegar junto a Django, seguir la guía `docs/DESPLIEGUE_VPS.md` de
BolsaTrabajo. `requirements.lock` fija las dependencias de Python 3.11 con
hashes; instalarlo con `uv pip sync --require-hashes requirements.lock`.

La cancelación de Django se entrega a `POST /api/v1/matches/{request_id}/cancel`
con el token de servicio. Impide publicar un resultado tardío y continuar los
siguientes lotes; una llamada ya enviada al proveedor puede terminar en curso.

La extracción PDF/DOCX/ODT corre en un proceso separado con timeout y memoria
limitada. Se validan formato, tamaño y contenido antes de procesar el documento.

Este proyecto recibe CVs en formatos habilitados, extrae su texto y lo analiza
en segundo plano; luego envia el resultado JSON a otra aplicacion mediante un
callback HTTP.

## Que ocurre cuando llega un CV

```text
Aplicacion origen
   | POST /api/v1/cv-analyses (archivo + IDs, multipart/form-data)
   v
FastAPI valida el archivo y publica una tarea en Redis
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

`services/job_store.py` conserva el registro recuperable e idempotente de
extracciones y sí participa del flujo actual. El almacenamiento de artefactos
privados y el proceso de extracción están en `cv_artifacts.py` y
`document_extractor_runner.py`.

## Ejecutar la pila completa en local

La integración necesita este microservicio, la aplicación Django de
`repositorio/BolsaTrabajo`, Redis y un worker de Celery. Los pasos siguientes
parten de clones nuevos de ambos repositorios. Requieren Python 3.11 para la API, Docker y
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

#### Alternativa ligera con Gemini

Para usar Gemini Flash-Lite en extracción y compatibilidad, reemplaza la
selección de proveedor del ejemplo anterior por estos valores en el `.env`
privado; conserva los secretos de integración entre servicios:

```dotenv
LLM_PROVIDER=gemini
MATCH_PROVIDER=groq
MATCH_GROQ_MODEL=qwen/qwen3.8-27b
GEMINI_API_KEY=tu-clave-privada-de-ai-studio
GEMINI_MODEL=gemini-3.5-flash-lite
# Ejemplo para cuota verificada de 15 RPM / 250.000 TPM / 500 RPD.
GEMINI_REQUESTS_PER_MINUTE=12
GEMINI_INPUT_TOKENS_PER_MINUTE=200000
GEMINI_REQUESTS_PER_DAY=400
GEMINI_PAUSA_SEGUNDOS=0
GEMINI_MAX_INPUT_TOKENS_PER_REQUEST=60000
GEMINI_MAX_OUTPUT_TOKENS=16384
CV_PACKET_CHARACTERS=20000
CV_BLOQUE_CARACTERES=20000
CV_EXTRACTION_MAX_TOKENS=16384
```

Son límites locales conservadores, no cuotas garantizadas por Google. Consulta
RPM, TPM y RPD efectivos en Google AI Studio. Una clave nueva del mismo proyecto
comparte cuotas. La integración usa pensamiento mínimo, JSON validado localmente
y espera los 429 según las indicaciones del proveedor. El modelo selecciona IDs
de citas literales preparadas por el servidor; un ID inválido no se convierte
en evidencia. Un fragmento inválido admite un único reintento de corrección.

Los CV se procesan de a uno mediante un lease global Redis, con concurrencia y
prefetch uno en Celery. La reserva atómica controla RPM, TPM de entrada estimados
y RPD; la ventana diaria local es móvil de 24 horas. Las esperas diarias liberan
el worker y reprograman el trabajo. Una hoja válida sin posibles duplicados se
ensambla en código; múltiples hojas o ambigüedad conservan consolidación LLM.
Se divide antes de llamar si la entrada supera su presupuesto, o después si la
salida queda truncada. Los callbacks reciben prioridad para entregar resultados.

`MATCH_PROVIDER` es independiente de `LLM_PROVIDER`: en esta configuración Gemini
extrae CV y Groq evalúa compatibilidad. Configurá `GROQ_API_KEY` en el `.env`
privado. En Django usa `MATCH_MODEL_VERSION=groq:qwen/qwen3.8-27b:strict-low`;
`scripts/configure_ia_match.py` sincroniza ese valor sin cambiar el proveedor
de extracción. Reinicia API, worker y procesos Django después de cambiar la
configuración, cuando no haya trabajos en curso. No guardes claves en código ni informes.

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

FastAPI debe responder `{"message":"La API de análisis de CV está disponible."}`; Django,
un `200`. El worker debe mostrar `celery@... ready.`.

## Contrato HTTP de entrada

`POST /api/v1/cv-analyses` recibe un cuerpo `multipart/form-data`:

| Campo | Tipo | Descripción |
| --- | --- | --- |
| `analysis_id` | entero | ID de la solicitud creada por Django. |
| `candidate_id` | entero | ID del postulante. |
| `cv` | archivo | PDF, DOCX, ODT o TXT; DOC/RTF si están habilitados; máximo 5 MiB. |

FastAPI devuelve la respuesta antes de extraer el texto. El worker de Celery
extrae el texto y realiza el análisis con Groq en segundo plano.

Respuesta inmediata (`202 Accepted`):

```json
{
  "job_id": "id-generado-por-celery",
  "status": "queued"
}
```

`analysis_id` y `candidate_id` deben ser mayores a cero; el archivo debe ser un
archivo admitido, no vacío y de hasta 5 MiB. FastAPI responde en español con un
error `4xx` si no se cumplen estas condiciones.

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
# Rama ia-match

Integra la extracción idempotente de `ia-aplicada` con el aviso de Benja.
Expone `POST /api/v1/cv-analyses`, `/offer-analyses` y `/profile-normalizations`
autenticados por Bearer token. El callback firmado guarda perfiles versionados
en Django; después se publica la notificación Redis para `user_<user_id>`.

El porcentaje se calcula con Groq dentro de esta misma API. CV, ofertas, normalización y compatibilidad comparten servidor en 8001, token de servicio y worker Celery. Para iniciar la pila:
`C:\IEN\repositorio\BolsaTrabajo\scripts\start_ia_match.ps1`.

## API única: CV y compatibilidad

La rama `ia-match` sirve todas las operaciones desde `app.main:app`, puerto 8001.

- `POST /api/v1/cv-analyses`: analiza un CV.
- `POST /api/v1/offer-analyses`: estructura una oferta.
- `POST /api/v1/profile-normalizations`: normaliza un perfil.
- `POST /api/v1/matches`: inicia una comparación.
- `GET /api/v1/matches/{request_id}`: recupera su estado y porcentaje.
- WebSocket `/api/v1/ws/v1/matches`: inicia o retoma una comparación desde Django.

Las rutas de análisis y comparación usan el mismo `SERVICE_API_TOKEN`. El worker
`celery -A app.core.celery_app.celery_app worker --pool=solo` procesa ambas clases
de trabajo. `MATCH_GROQ_MODEL` permite elegir otro modelo para compatibilidad;
si se omite usa `CV_MODEL`. El limitador Redis de Groq se comparte con extracción
solo cuando la extracción también usa Groq. La evaluación usa JSON Schema estricto
en Qwen 3.8, procesa hasta cuatro requisitos por llamada y calcula puntos por
requisito con los pesos publicados en Django.
El puerto 8002 y el paquete `jev_match_api` ya no forman parte del arranque.

## Prompt y cálculo de compatibilidad v3

- `app/prompts/cv_extraction.v2.packets-soft.v8.system.md`, `cv_consolidation.v2.refs.system.md` y `offer_extraction.v2.system.md`
  estructuran CV y ofertas de cualquier rubro con textos visibles en español.
  Las habilidades conservan evidencia y citas del documento original.
- `GroqClient.evaluate_v2` devuelve un juicio por identificador de requisito.
  El servidor valida las referencias de evidencia y conserva la clasificación
  obligatorio/deseable confirmada en Django.
- `app/services/match_scoring.py` calcula en Python el total y el desglose a
  partir de esos juicios. Los porcentajes llegan en `scoring_config` desde una
  configuración publicada de Django admin; no los decide el modelo.
- Las siete dimensiones son competencias técnicas, habilidades blandas,
  experiencia, formación/certificaciones, idiomas, disponibilidad/modalidad y
  deseables. La configuración debe sumar exactamente 10.000 centésimas de
  porcentaje (100%). Los valores iniciales son 35/15/20/10/5/5/10.
- Una dimensión sin requisitos no aporta puntos gratuitos: se redistribuyen
  proporcionalmente los pesos aplicables. La ausencia de evidencia de un
  requisito solicitado permanece como `unknown` y puntúa cero en el encaje
  acreditado, diferenciada de un incumplimiento explícito.
- El estado de obligatorios se informa por separado. La rúbrica v2 no aplica
  el antiguo techo fijo de 40%. El circuito v1 se conserva para compatibilidad
  de lectura y trabajos anteriores, no para nuevas solicitudes de Django.
- Se envía el perfil una sola vez y un catálogo de referencias; el presupuesto
  de respuesta se ajusta a la cantidad de requisitos. Los errores 429 respetan
  `Retry-After` y reprograman el trabajo sin repetir llamadas inmediatamente.

El paquete `ien_contracts/` se incluye en el repositorio. No requiere la carpeta
externa `C:\IEN\contracts`. Mantener sus modelos y el helper de límites de
procesos sincronizados con Django. Los nuevos pedidos usan `match.v2`, incluyen
la fecha de evaluación y guardan configuración, modelo y versiones utilizados.

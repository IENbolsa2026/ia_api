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
`CV_PACKET_CHARACTERS` (500 por defecto en código y 20000 en el ejemplo actual,
limitado por `CV_BLOQUE_CARACTERES`).
Cada llamada utiliza `CV_EXTRACTION_MAX_TOKENS` (950 por defecto en código;
16384 en el ejemplo Gemini actual). Si una salida
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

### 2. Configuración: de dónde sale cada valor del .env

Copiar la plantilla desde `ia_api/backend`:

```bash
cp .env.example .env
```

En Windows: `Copy-Item .env.example .env`. La plantilla representa el flujo
actual, **Gemini para extraer CV y Groq para matches**, con puertos locales y
sin secretos reales. No sobrescribir un `.env` existente sin conservar su
configuración; este archivo permanece fuera de Git.

#### Paso 1: crear las claves de los proveedores

Para **Gemini**:

1. Entrar en [Google AI Studio](https://aistudio.google.com/) con la cuenta del
   proyecto y abrir la sección de claves API.
2. Seleccionar o crear el proyecto y crear su clave API. Pegar el valor en
   `GEMINI_API_KEY`. Ver el [procedimiento oficial](https://ai.google.dev/gemini-api/docs/api-key).
3. Elegir un modelo disponible para ese proyecto y copiar su ID exacto en
   `GEMINI_MODEL`. El nombre del ejemplo es el usado por la configuración actual
   del proyecto y debe verificarse para tu cuenta.
4. Abrir la información de límites de AI Studio y revisar RPM, TPM y RPD del
   modelo. Ajustar las variables del paso 4 a esos valores; la
   [documentación de cuotas](https://ai.google.dev/gemini-api/docs/rate-limits)
   explica su alcance por proyecto.

Para **Groq**, entrar en [API Keys](https://console.groq.com/keys), crear una
clave del servicio y pegarla en `GROQ_API_KEY`. Copiar el ID del modelo de match
desde [los modelos de Groq](https://console.groq.com/docs/models) a
`MATCH_GROQ_MODEL`. Conservar `MATCH_PROVIDER=groq`; `LLM_PROVIDER=gemini` es
independiente. `CV_MODEL` solo se usa al cambiar la extracción a Groq, y como
fallback si no se define un modelo específico de match.

Las claves de estos proveedores van solo en la API. No son tokens del usuario,
ni claves de CORS, ni los secretos internos que conectan Django con FastAPI.

#### Paso 2: generar y compartir los secretos internos

Ejecutar dos veces y usar resultados distintos:

```bash
python3.11 -c "import secrets; print(secrets.token_urlsafe(48))"
```

En Windows, reemplazar `python3.11` por `py -3.11`.

| Resultado | API | Django |
| --- | --- | --- |
| Primero, token de servicio | `SERVICE_API_TOKEN` | Copiarlo en `IA_API_TOKEN` y `MATCH_API_TOKEN`. |
| Segundo, firma del callback | `CV_RESULT_CALLBACK_HMAC_SECRET` | Copiarlo en `IA_CALLBACK_HMAC_SECRET`. |

Django también necesita su propio `SECRET_KEY`, generado por separado. Los
ejemplos nuevos no piden `JWT_SECRET_KEY`, `SERVICE_USER`, `SERVICE_PASSWORD`,
`ACCESS_TOKEN_EXPIRE_MINUTES` ni `CV_RESULT_CALLBACK_SECRET`: no se usan como
credenciales del flujo actual. La ruta HTTP usa Bearer con `SERVICE_API_TOKEN`
y el callback usa HMAC. El código conserva compatibilidad con el secreto de
callback antiguo para instalaciones anteriores.

`CV_RESULT_CALLBACK_URL` es la dirección donde Django recibe el resultado:
`http://127.0.0.1:8000/ia/llegue/` en local. En la VPS con Caddy y redirección
HTTPS usar `http://127.0.0.1:8080/ia/llegue/`. El host `127.0.0.1` debe estar
permitido por Django. Si los servicios están en máquinas/contenedores distintos,
usar una dirección que el worker pueda alcanzar.

#### Paso 3: CORS, modo y Redis

**CORS no tiene una clave.** `BACKEND_CORS_ORIGINS` es una lista JSON de
orígenes autorizados para llamadas desde el navegador. Un origen combina
esquema, dominio y puerto; por ejemplo `https://app.example.com`, sin ruta.
Ver [CORS en FastAPI](https://fastapi.tiangolo.com/tutorial/cors/).

En esta aplicación Django llama a la API entre servidores, por lo que usar
`BACKEND_CORS_ORIGINS=[]`. Un frontend separado podría necesitar su propio
origen en esa lista, pero CORS no reemplaza Bearer ni permite publicar el token
de servicio en JavaScript del navegador.

| Variable | Origen del valor |
| --- | --- |
| `PROJECT_NAME` | Nombre que elegís para la documentación de la API. No es el ID de Google Cloud. |
| `MODE` | `development` en local; `production` en la VPS. |
| `CELERY_BROKER_URL` | Tu servidor Redis, base lógica `/0`, usada para la cola. |
| `CELERY_RESULT_BACKEND` | El mismo Redis, base `/1`, usada por los resultados Celery. |
| `JOB_STORE_REDIS_URL` | Redis `/2`: trabajos y limitador de cuota. |
| `CHANNEL_REDIS_URL` | Redis `/3`: misma URL que Django para notificaciones. |
| `MATCH_REDIS_URL` | Redis `/4`: estado de compatibilidades. |
| `JOB_TTL_SECONDS` | Tiempo de conservación de trabajos; `2592000` son 30 días. |

Con el Redis local del Compose, conservar `redis://127.0.0.1:6379/N`. Para una
instancia administrada, copiar host, puerto, contraseña y esquema de conexión
del proveedor, manteniendo separadas las bases que soporte. Redis no genera
una clave API de Gemini o de CORS.

#### Paso 4: presupuestos de IA

Los valores del ejemplo reflejan el ajuste local del proyecto. Son límites de
la aplicación; **no conceden cuota** al proveedor ni acreditan que tu cuenta la
tenga. Si tu panel tiene límites menores, bajarlos antes de usar el servicio.

| Variables | Cómo elegir el valor |
| --- | --- |
| `GEMINI_REQUESTS_PER_MINUTE` | Máximo local de solicitudes por minuto, dentro del RPM del proyecto/modelo. |
| `GEMINI_INPUT_TOKENS_PER_MINUTE` | Presupuesto local de entrada estimada por minuto; compararlo con el TPM efectivo. |
| `GEMINI_REQUESTS_PER_DAY` | Tope local de solicitudes diarias; el programa usa una ventana móvil de 24 horas. |
| `GEMINI_MAX_INPUT_TOKENS_PER_REQUEST` | Presupuesto de entrada por llamada, compatible con el modelo y su cuota. |
| `GEMINI_MAX_OUTPUT_TOKENS` | Límite de salida pedido al proveedor; no superar lo admitido por el modelo. |
| `GEMINI_PAUSA_SEGUNDOS` | Pausa local adicional entre llamadas; el ejemplo usa `0` y el limitador Redis controla la cuota. |
| `CV_PACKET_CHARACTERS` / `CV_BLOQUE_CARACTERES` | Tamaño de paquetes y tope de fragmentos, en caracteres; el ejemplo usa `20000`. No son tokens. |
| `CV_EXTRACTION_MAX_TOKENS` | Presupuesto de salida de extracción; el ajuste actual usa `16384` y se divide el paquete cuando es necesario. |
| `GROQ_REQUESTS_PER_MINUTE` | Máximo local de llamadas Groq; verificar los límites de la cuenta/modelo en su consola. |
| `GROQ_PAUSA_SEGUNDOS` / `GROQ_MAX_TOKENS` | Pausa entre consultas y máximo de salida Groq. |
| `MATCH_GROQ_MAX_TOKENS` | Presupuesto de salida del comparador; conservar el ejemplo salvo ajuste verificado. |

Una nueva clave del mismo proyecto no resetea sus cuotas. Si cambiás de
proveedor de extracción, también hay que adaptar modelo y presupuestos. OpenAI
es una alternativa implementada en código, pero no forma parte del ejemplo
activo Gemini/Groq; requiere su propia configuración específica.

#### Paso 5: archivos y componentes opcionales

| Variables | Qué representan |
| --- | --- |
| `CV_MAX_PDF_BYTES` | Tamaño máximo de archivo; `5242880` son 5 MiB. |
| `CV_MAX_PDF_PAGES` / `CV_MAX_TEXT_CHARACTERS` | Máximo de páginas PDF y longitud de texto; `20` y `100000` en el ejemplo. |
| `CV_MAX_ARCHIVE_BYTES` / `CV_MAX_ARCHIVE_ENTRIES` | Tamaño descomprimido y cantidad de entradas máximas para documentos Office. |
| `CV_MAX_UPLOAD_BODY_OVERHEAD` | Margen de bytes para campos/cabeceras multipart además del archivo. |
| `CV_EXTRACT_TIMEOUT_SECONDS` | Tiempo máximo para el proceso de extracción documental. |
| `CV_ARTIFACT_DIR` | Directorio privado elegido por vos; relativo a `backend` o una ruta absoluta con permisos para API y Celery. No publicarlo como archivos estáticos. |
| `CV_LEGACY_CONVERTER_IMAGE` | Tag de la imagen Docker que construís con `docker/cv-converter.Dockerfile`. Vacío deshabilita DOC/RTF; PDF/DOCX/ODT/TXT siguen funcionando. |
| `CV_DOCKER_COMMAND` / `CV_LEGACY_CONVERSION_TIMEOUT_SECONDS` | Ejecutable Docker y timeout del conversor opcional. |
| `CV_RAG_CONTEXT_MAX_CHARACTERS` | Máximo de contexto de catálogo entregado a la extracción. |
| `SKILL_CATALOG_DATABASE_URL` | URL PostgreSQL/pgvector del catálogo opcional; vacío usa el catálogo JSON. No es la BD de negocio de Django. |
| `SKILL_CATALOG_DB_PASSWORD` | Contraseña que generás para el rol `skill_catalog` al usar `docker-compose.rag.yml`. Solo Compose lee esta variable. |
| `SKILL_CATALOG_EMBEDDING_MODEL` / `SKILL_CATALOG_EMBEDDING_DIMENSIONS` | ID del modelo de embeddings y dimensiones de sus vectores; deben coincidir con la importación y el esquema del catálogo. |

Para RAG, generar una contraseña distinta, levantar la base según la sección
«Contexto RAG de habilidades» y formar la URL con el usuario `skill_catalog`,
la contraseña elegida, `127.0.0.1:5434` y la base `ien_skill_catalog`. Si la
contraseña incluye caracteres reservados, codificarla como componente de URL.
Ejecutar la importación indicada en esa sección antes de habilitar el catálogo.

#### Paso 6: cambios de VPS y comprobación

Cambiar `MODE=production` y el callback a `8080`. En Django configurar
`DEBUG=false`, PostgreSQL, el dominio HTTPS, el proxy y SMTP según su
[guía VPS](https://github.com/Deathstone16/BolsaTrabajo/blob/develop/docs/DESPLIEGUE_VPS.md).
El paso a paso del correo y de la contraseña de aplicación Gmail está en el
[README de Django](https://github.com/Deathstone16/BolsaTrabajo/blob/develop/README.md).

Completar los dos `.env` y ejecutar desde el entorno Django
`python deploy/check_config.py` con las rutas de VPS por defecto, o pasar
`--django-root` y `--api-root` si la distribución de carpetas es otra. Es un
verificador de **producción**, no un validador de los ejemplos de desarrollo.
No imprime secretos ni confirma claves externas. Reiniciar API, Celery,
Django y los despachadores tras modificar la configuración, cuando no haya
trabajos en curso.


### 3. Preparar Django

En `repositorio/BolsaTrabajo`, crea su entorno, instala dependencias, configura
el `.env` y ejecuta migraciones. Los valores necesarios son:

```dotenv
IA_API_URL=http://127.0.0.1:8001/api/v1/cv-analyses
IA_API_TOKEN=el-mismo-SERVICE_API_TOKEN-de-la-API
MATCH_API_TOKEN=el-mismo-SERVICE_API_TOKEN-de-la-API
IA_CALLBACK_HMAC_SECRET=el-mismo-CV_RESULT_CALLBACK_HMAC_SECRET-de-la-API
CHANNEL_REDIS_URL=redis://127.0.0.1:6379/3
```

Consulta el [README de Django](https://github.com/Deathstone16/BolsaTrabajo/blob/develop/README.md)
para los comandos completos.

### 4. Iniciar los servicios

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
`status: "failed"`, `result: null` y un mensaje en `error`. La configuración
actual usa `CV_RESULT_CALLBACK_HMAC_SECRET` y envía:

```text
X-IA-Event-Timestamp: marca-de-tiempo-del-evento
X-IA-Callback-Signature: firma-hmac-del-timestamp-y-cuerpo
```

La otra aplicación verifica la firma con `IA_CALLBACK_HMAC_SECRET` antes de aceptar el callback y debe
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
- **Gemini/Groq**: realizan la extracción y la compatibilidad según `LLM_PROVIDER` y `MATCH_PROVIDER`.
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
## API única: CV y compatibilidad

La API actual sirve todas las operaciones desde `app.main:app`, puerto 8001.

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

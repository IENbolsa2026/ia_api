# Guía de flujo — pdf_analysis_service

Guía para **leer este código como si lo hubieras escrito vos** y para seguir el
flujo de datos en vivo con los `print` que están marcados `[FLUJO]`.

---

## 1. La regla de las capas (lo más importante)

Cada archivo solo llama a los archivos de **abajo**, nunca a los de arriba:

```
        ┌───────────────────────────────────────────────┐
        │  routers/        reciben el HTTP, validan,     │
        │                  crean el job y responden      │   ← encima
        ├───────────────────────────────────────────────┤
        │  tasks/          saben el ORDEN de las etapas  │
        ├───────────────────────────────────────────────┤
        │  services/       lógica pura (sin HTTP,        │
        │                  sin FastAPI)                  │   ← abajo
        └───────────────────────────────────────────────┘

        compartidos por todos:
          core/connection_manager.py → estados + websockets (en memoria)
          schemas/analisis.py        → contratos JSON (Pydantic)
          config.py                  → settings (env vars)
```

Para saber "de dónde viene" una función, buscás quién la llama con `rg nombre_de_la_funcion app/`. La respuesta es siempre una capa arriba.

---

## 2. Flujo de arranque (una vez, al levantar el server)

```
uvicorn app.main:app --reload
        │
        ▼
app/main.py:14     app = FastAPI(...)                  ← se crea el servidor
app/main.py:32     app.include_router(pdf.router)       ← registra POST /procesar-pdf
app/main.py:33     app.include_router(websocket.router) ← registra WS  /ws/{job_id}
app/main.py:36     GET /health                          ← health check
```

`main.py` **no tiene lógica**: solo arma la app y registra los routers. Por eso el
import path es `app.main:app` — uvicorn importa el objeto `app` y listo.

---

## 3. Flujo de una request: POST /procesar-pdf

Este es el viaje completo de un PDF, de punta a punta:

```
 CLIENTE (browser / Django)                       SERVIDOR (uvicorn + FastAPI)
   │
   │ POST /procesar-pdf
   │ body multipart: file=<PDF>, job_id?=...
   ▼
   │  app (main.py)  —— match de ruta ──────────►  routers/pdf.py::procesar_pdf()
   │                                                 │  FastAPI parsea el body usando
   │                                                 │  los type hints:
   │                                                 │    file:   Annotated[UploadFile, File(...)]
   │                                                 │    job_id: Annotated[str|None, Form(...)]
   │                                                 │
   │                                                 ▼
   │                                            valida: content-type / vacío / tamaño
   │                                                 (400 o 413 si algo falla)
   │                                                 ▼
   │                                            manager.crear_job(job_id, nombre)
   │                                                 │  guarda estado en memoria:
   │                                                 │  {estado: PENDIENTE, progreso: 0, ...}
   │                                                 ▼
   │                                 asyncio.get_running_loop().create_task(...)
   │                                                 │  encola la tarea EN BACKGROUND
   │                                                 │  y NO espera (clave del diseño)
   │  ◄──────────────────────────────── 202 Accepted │
   │  {job_id, estado: "PROCESANDO"}                 │
   │   (la request HTTP ya terminó acá)              ▼
   │                                          tasks/procesar_pdf.py (corre aparte)
   │
   │  mientras, el navegador se conecta a  ◄───────  WS /ws/{job_id}
   │  y recibe en vivo: snapshot, progreso,
   │  resultado o error
```

### Con qué sale la request HTTP

`routers/pdf.py` → `ProcesarPdfResponse(job_id, estado=PROCESANDO)`. FastAPI usa
`response_model=ProcesarPdfResponse` para validar el JSON de salida y para
documentarlo en `/docs`. **El cliente no espera el análisis**: se lleva un
"boleto" (`job_id`) y escucha el resultado por websocket.

---

## 4. Flujo de la tarea en background

`tasks/procesar_pdf.py` es el **cerebro**: conoce el orden de las etapas.

```
        ESTADO EN MEMORIA (manager)                          SERVICIOS (hilos)
      {progreso: 5%}   ◄──── publicar("Leyendo el PDF…")
      {progreso: 40%}  ◄──── publicar("consultando a Groq…")
      {progreso: 90%}  ◄──── publicar("avisando a Django…")
      {COMPLETADO,100%}◄──── publicar("tipo": "resultado")
```

Cada `publicar(...)` hace 3 cosas:

```
publicar()  →  arma un WebSocketMessage (schema)
            →  manager.publicar(job_id, payload)
                  ├── 1. guarda el estado en memoria (dict del job)
                  └── 2. broadcast() ← envía el JSON a los websockets conectados
```

La secuencia de funciones llamadas (los pasos del 1 al 3 en la consola):

| Paso | `await asyncio.to_thread(...)` | Qué hace | Devuelve |
|------|--------------------------------|----------|----------|
| 1/3 | `pdf_service.extraer_texto(pdf_bytes)` | pypdf abre el PDF y junta el texto página por página (`--- Página N ---`) | `str` |
| 1b  | `pdf_service.resumir_texto(texto)` | recorta al límite de contexto si hace falta | `str` |
| 2/3 | `groq_service.analizar_texto(texto)` | llama a Groq con el prompt (la "skill"), valida el JSON contra `AnalisisResultado` | `AnalisisResultado` |
| 3/3 | `django_client.notificar_resultado(...)` | hace `POST` al webhook de Django | `bool` |

### Por qué `asyncio.to_thread(...)`

El código es asíncrono (`async def`), pero pypdf, Groq y httpx son **funciones
bloqueantes**. `asyncio.to_thread` las corre en un hilo aparte para que NO congele
el loop de eventos (el que atiende a todos los demás requests). Regla práctica:
solo `await` lo que realmente bloquea.

### El paso "objeto → JSON"

```python
resultado = await asyncio.to_thread(groq_service.analizar_texto, texto)   # objeto AnalisisResultado
resultado_json = resultado.model_dump(mode="json")                        # dict serializable
```

`model_dump(mode="json")` es el botón de Pydantic que convierte **objeto → dict**.
El inverso es `model_validate({"..."})` o `model_validate_json('{...}')`: **JSON → objeto**.
Lo vas a usar siempre, en este y en cualquier proyecto FastAPI.

---

## 5. Qué pasa si algo falla

```
                                        ┌─ extraer_texto  → PdfError   (PDF vacío / escaneado / corrupto)
tasks exception  ←─ cualquiera de ──────┼─ analizar_texto → GroqError  (sin key / JSON inválido / API caída)
     │                                   └─ notificar      → DjangoClientError
     ▼
  1. logger.exception(...)  (traceback en consola)
  2. try:  django_client.notificar_error(...)   ← avisar el error a Django si se puede
  3. publicar("tipo": "error", estado=ERROR)    ← avisar por websocket + persistir estado
```

El job queda con `{estado: ERROR, error: "..."}` en memoria, y cualquier websocket
conectado a ese `job_id` recibe el mensaje de error.

---

## 6. El websocket (del navegador)

```
CLIENTE                                            SERVIDOR
  ├────────────────── WS /ws/{job_id} ───────────► routers/websocket.py
  │  handshake → manager.connect(job_id, ws)       (conocido el job? si no → close 4404)
  │  ◄─ snapshot (estado actual del job)           (por si se conectó tarde)
  │
  │  (de acá en adelante recibe solo lo que la     ┌─ cada publicar() de la tarea
  │   tarea publique, sin pedir nada)              └─ broadcast() → manda el JSON
  │
  ├── envía "ping" ──────────────────────────────►  responde con el snapshot actualizado
```

El `connection_manager` es el **puente invisible**: la tarea nunca sabe quién está
escuchando; solo publica por `job_id`. Por eso la capa `tasks` no conoce nada de websockets.

---

## 7. Orden de lectura recomendado

1. `routers/pdf.py` — entrada. Todo es FastAPI puro.
2. `tasks/procesar_pdf.py` — el cerebro: el orden de las etapas.
3. `services/pdf_service.py` → `groq_service.py` → `django_client.py` — las cajas negras.
4. `schemas/analisis.py` — los contratos. Cuando un import traiga `ProcesarPdfResponse`,
   `WebSocketMessage`, `AnalisisResultado`, es de acá.
5. `core/connection_manager.py` — la caja de estado compartida.
6. `config.py` y `main.py` — al final (no tienen historia que contar).

---

## 8. Cómo seguirlo en vivo (con los prints)

Levantá el server, subí un PDF desde `http://localhost:8000/docs` y mirá la consola:

```
[FLUJO] POST /procesar-pdf recibe 'test.pdf' de 12345 bytes · job_id=...
[FLUJO] job ... registrado en memoria (estado PENDIENTE).
[FLUJO] create_task lanzó la tarea en background → respondo 202 al cliente

[FLUJO] === TAREA procesar_pdf('...') arranca en background ===
[FLUJO] pdf_service.extraer_texto: pypdf abrió 2 páginas
[FLUJO] manager.publicar: ... · progreso · estado=PROCESANDO · 5%
[FLUJO] etapa 1/3: texto extraído y recortado → 4321 caracteres
[FLUJO] manager.publicar: ... · progreso · estado=PROCESANDO · 40%
[FLUJO] groq_service.analizar_texto: 4321 chars → modelo llama-3.3-70b-versatile
[FLUJO] groq_service._llamar: respuesta cruda de 980 chars
[FLUJO] etapa 2/3: Groq respondió OK → resumen: 'Resumen del documento...'
[FLUJO] manager.publicar: ... · progreso · estado=PROCESANDO · 90%
[FLUJO] etapa 3/3: resultado enviado a Django (o aviso omitido sin URL)
[FLUJO] manager.publicar: ... · resultado · estado=COMPLETADO · 100%
[FLUJO] === TAREA terminada: COMPLETADO publicado por websocket ===
```

Para verlo con websocket conectado: abrí `http://localhost:8000/ws-test`, pegá el
`job_id` que te devolvió el POST y conectáte **antes** de procesar otro PDF.

Con un `.txt` en vez de PDF verás el cortocircuito: el `HTTPException` del router
(400) y **ningún** print de la tarea. Con la `GROQ_API_KEY` placeholder verás el
camino de error: `[FLUJO] === TAREA con ERROR: GROQ_API_KEY no está configurada ===`.

### Cuándo sacar los prints

Cuando ya no los necesites (o antes de subir a producción):

```bash
# borrarlos todos a mano, o usar sed para quitar las líneas que empiezan con print("[FLUJO]
grep -rn '\[FLUJO\]' app/
```

---

## 9. Conceptos de FastAPI que estás viendo en este código

| Concepto | Dónde | Qué es |
|----------|-------|--------|
| `APIRouter` | `routers/*.py` | módulos de rutas reutilizables |
| `include_router` | `main.py` | montar routers en la app |
| `@router.post("/ruta", response_model=...)` | `pdf.py:15` | define método + path + contrato de salida |
| `Annotated[UploadFile, File(...)]` / `Form(...)` | `pdf.py:22` | FastAPI parsea el body según el type hint |
| `HTTPException(status, detail)` | `pdf.py:29` | respuesta de error con su status |
| `status.HTTP_202_ACCEPTED` | `pdf.py:18` | constantes de status HTTP |
| `asyncio.to_thread` | `tasks/*.py` | correr código bloqueante sin congelar el loop |
| `create_task` | `pdf.py:49` | lanzar trabajo en background "fire and forget" |
| `response_model` + schemas Pydantic | `pdf.py:17`, `schemas/*` | validación automática + documentación en /docs |
| `model_dump(mode="json")` | `tasks/*.py` | objeto Pydantic → dict/JSON |
| websocket `@router.websocket` | `websocket.py` | conexión bidireccional en tiempo real |
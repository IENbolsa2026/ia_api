# Correcciones del análisis de CV en producción

## Definición y autorización

El 19/11/2026 el usuario autorizó corregir los problemas diagnosticados,
exclusivamente en la API, e informar todos los archivos modificados.
Esta autorización cubre la definición y el plan siguientes. Django queda fuera
del cambio. No se despliega ni se modifica información de producción.

Problemas reproducidos: columnas PDF intercaladas; recuperación de estudios
espurios que invalida hechos correctos; presupuesto de entrada presentado como
truncamiento de salida; repetición de validaciones locales deterministas;
Redis de notificaciones apuntando al propio contenedor; interpolación de `$`
en secretos de Compose.

## Plan aprobado por la autorización del usuario

1. Corregir la lectura de columnas y recuperar estudios solo con evidencia
   válida, conservando las comprobaciones de graduación.
2. Separar límites de entrada y salida; dividir solo cuando el tamaño del texto
   pueda resolver el exceso. Entregar errores permanentes sin reanalizar.
3. Corregir el valor de Redis en Compose y documentar secretos y configuración
   Gemini, sin cambiar entornos privados.
4. Añadir regresiones anónimas, ejecutar las pruebas y revisar el diff.

## Fuera de alcance

La reutilización de eventos de salida de Django tras borrar análisis, la
supervisión de su dispatcher y la configuración de su consumidor de avisos
requieren cambios en BolsaTrabajo. Se informarán como pendientes.
La lectura de `BolsaTrabajo/apps/ia/workflow.py:111` confirma que
`EventoSalida.objects.get_or_create` puede recuperar el evento anterior por
clave sin actualizar su `payload.job_id`. No se modificó ese archivo.

## Verificación y cierre

Implementado exclusivamente en la API:

- Lectura de columnas con líneas desalineadas y distinta cantidad de texto.
- Recuperación conservadora de estudios y cursos: cada agregado se valida
  individualmente. Se reconoce «Educación Secundaria» sin inventar otra carrera
  ni convertir instituciones y continuaciones de nombres en títulos.
- Error de entrada independiente del truncamiento de salida, con presupuesto
  estimado y límite. Solo se divide si el texto puede caber; las instrucciones
  que no caben fallan sin peticiones HTTP ni reservas de cuota.
- Validación final local determinista sin reintentos de IA; callback de error
  terminal y eliminación del artefacto privado según el flujo existente.
- Presupuesto de entrada Gemini predeterminado de 20000 en código y plantilla.
- Redis de avisos interno por defecto en Compose; override explícito mediante
  `DOCKER_CHANNEL_REDIS_URL` para instalaciones con Redis externo.
- Documentación de secretos literales con `$`, selección de proveedor,
  configuración de Gemini y diagnóstico de los estados observados.

Comprobaciones ejecutadas el 19/11/2026:

- Suite completa: **276 pruebas y 232 subtests aprobados**. Después se agregó
  una regresión de caída del Redis de avisos: **5 pruebas de notificaciones
  aprobadas**, incluidas las cuatro anteriores. Total: 277 pruebas distintas.
- Primera selección específica: 48 pruebas y 34 subtests aprobados.
- PDF real proporcionado, leído sin modificarlo: una página, separación de
  columnas en x=295.368; exactamente dos estudios recuperados, tecnicatura en
  curso y secundario completo. No se agregó el CV al repositorio.
- Flujo del PDF real por el subproceso de extracción, dos paquetes,
  consolidación y perfil validado con respuestas de IA simuladas: completado.
  No se realizaron llamadas facturables a proveedores.
- `docker compose config --format json` con entorno temporal sintético:
  API y worker usan Redis interno aunque `CHANNEL_REDIS_URL` diga localhost;
  el override externo funciona; el secreto entre comillas simples con `$`
  no provoca advertencias de interpolación.
- `git diff --check`: sin errores.

Las pruebas corrieron en Windows con Python 3.13.2. Las dependencias faltantes
de pruebas se instalaron en un directorio temporal, sin cambiar los entornos
del proyecto. Asyncio requiere sockets locales que el sandbox de Windows
bloquea, por lo que las pruebas asíncronas se ejecutaron fuera del sandbox.
Los servicios externos se simularon. No se desplegó ni se probó contra el VPS;
el contenedor de producción utiliza Python 3.11.

### Archivos modificados

- `.env.example`
- `app/core/config.py`
- `app/services/cv_education_guard.py`
- `app/services/cv_packets.py`
- `app/services/document_text.py`
- `app/services/gemini_client.py`
- `app/services/rate_limiter.py`
- `app/tasks/cv_analysis_tasks.py`
- `docker-compose.prod.yml`
- `docs/DEPLOY_COMPOSE.md`
- `docs/changes/001-cv-production-recovery.md` (nuevo)
- `tests/test_cv_education_grounding.py`
- `tests/test_cv_notifications.py`
- `tests/test_cv_single_leaf_fastpath.py`
- `tests/test_document_text.py`
- `tests/test_gemini_admission.py`
- `tests/test_provider_output_terminal.py`

La conformidad final corresponde al usuario después de revisar la entrega.

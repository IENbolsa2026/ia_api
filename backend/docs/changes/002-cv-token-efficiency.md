# Eficiencia de tokens en CV extensos

## Definición, aprobación y plan

El 20/11/2026 el usuario autorizó mejorar la API cuidando el consumo de tokens.
La autorización cubre este plan y su implementación. No se modifica Django,
no se despliega y no se realizan llamadas pagas.

1. En Gemini, enviar el texto una vez, con referencias literales, y enviar el
   esquema únicamente mediante el mecanismo estructurado del proveedor.
2. Compactar las instrucciones sin quitar taxonomía, reglas de niveles,
   separación de hechos, estados académicos ni validación local.
3. Consolidar los hechos mediante un plan determinista conservador, sin otra
   llamada a la IA y sin límites de entrada por acumulación de fragmentos.
4. Reconciliar reservas locales con el consumo informado por Gemini, sin
   aumentar las cuotas configuradas ni repetir llamadas por fallos de registro.
   Ceder el worker durante las esperas de cuota, guardando los avances.
5. Verificar recuperación, aislamiento, documentos largos y ahorro mediante
   pruebas y simulaciones locales reproducibles.

## Compatibilidad

El perfil eficiente se identifica con una nueva versión interna del prompt.
Los manifiestos v13 siguen siendo válidos para su versión anterior, siempre
que archivo, proveedor/modelo y configuración coincidan. Se prueba tanto la
recuperación de un resultado completo como la consolidación pendiente.
Las rutas y contratos de Django no cambian. Las instrucciones de despliegue
deben contemplar terminar los trabajos activos antes de cambiar de versión.

## Verificación y cierre

Implementación terminada localmente; no se desplegó ni se realizaron operaciones
Git de escritura. No se cambió Django, su contrato compartido ni sus servicios.
Tampoco se cambiaron las cuotas, tamaños ni secretos del entorno de producción.

### Resultados medidos el 20/11/2026

Comparación reproducible con `python -B scripts/benchmark_cv_budget.py`, texto
técnico sintético, paquetes de 20000 caracteres y presupuesto de entrada de
20000 tokens estimados por solicitud:

| Caracteres | Extracciones antes | Extracciones ahora | Entrada estimada antes | Entrada estimada ahora | Reducción estimada |
|---:|---:|---:|---:|---:|---:|
| 20000 | 4 | 1 | 66501 | 16246 | 75,6% |
| 60000 | 13 | 4 | 212040 | 54008 | 74,5% |
| 100000 | 21 | 6 | 344952 | 86496 | 74,9% |

Antes se necesitaba además una solicitud de consolidación en estos ejemplos;
ahora se hacen cero. La tabla no incluye los tokens de esa consolidación, ni
salida ni reintentos. Son estimaciones locales, no tokens facturados ni tiempos
medidos contra Gemini. No demuestra equivalencia de calidad de un modelo real;
queda pendiente medir calidad, consumo y latencia reales después del despliegue.

Verificación final: **297 pruebas y 232 subtests aprobados** (11,53 s), incluyendo:

- PDF sintético válido de 20 páginas, parser real en subproceso y flujo completo
  con respuestas de IA simuladas. Texto de más de 60000 caracteres, página final
  presente, consolidación local y perfil validado.
- Simulación de 100000 caracteres con presupuesto real de transporte, cobertura
  completa, reanudación desde última hoja y resultado final sin volver a la IA.
- Conservación de todos los caracteres, Unicode, espacios, repetición y citas
  de estudios envueltas en varias líneas al anotar la fuente.
- Formación del PDF real proporcionado: tecnicatura en curso y secundario
  completo conservados; dos solicitudes simuladas y cero de consolidación;
  estimación de entrada total de 12072 tokens. No se agregó el CV al repositorio.
- Contradicciones de estudios, duraciones y niveles no se fuerzan a una unión.
  Si excedería ocho citas, se conservan varios registros válidos sin tirar
  evidencia. Se mantienen los máximos del contrato compartido.
- Dos respuestas de fragmento inválidas terminan sin cinco ciclos completos
  de Celery; un desborde del número de hechos divide el paquete sin pedir una
  segunda respuesta imposible del mismo tamaño.
- Esperas cortas de cuota ceden el worker; versiones antiguas conservan su
  política y aislamiento. Los controles de cobertura y manipulación de
  manifiestos siguen activos.
- Scripts Lua ejecutados en Redis en memoria con `fakeredis[lua]`: reconciliación
  de una sola reserva, conservación de timestamps/RPM/RPD, ajuste hacia arriba
  ante una subestimación y ausencia de resurrección de reservas vencidas.
- Una respuesta recibida no se vuelve a solicitar por errores al registrar la
  cuota. Las respuestas truncadas también contabilizan su entrada real.
- `git diff --check`: sin errores.

Entorno local: Windows, Python 3.13.2; producción usa Python 3.11. Las pruebas
asíncronas requirieron salir del sandbox por sus restricciones de sockets
locales. Las dependencias de prueba faltantes se instalaron exclusivamente en
un directorio temporal; ninguna dependencia nueva se agregó a producción.
Las pruebas de Lua se omiten si no están instalados `fakeredis` y `lupa`.

### Archivos modificados o nuevos (19)

- `README.md`
- `app/prompts/cv_extraction.v2.efficient.system.md` (nuevo)
- `app/services/cv_compact_contract.py`
- `app/services/cv_consolidation.py`
- `app/services/cv_packets.py`
- `app/services/cv_pipeline.py`
- `app/services/cv_request_budget.py` (nuevo)
- `app/services/cv_skill_taxonomy.py`
- `app/services/gemini_budget.py`
- `app/services/gemini_client.py`
- `app/services/skill_context.py`
- `app/tasks/cv_analysis_tasks.py`
- `docs/DEPLOY_COMPOSE.md`
- `docs/changes/002-cv-token-efficiency.md` (nuevo)
- `scripts/benchmark_cv_budget.py` (nuevo)
- `tests/test_cv_token_efficiency.py` (nuevo)
- `tests/test_gemini_admission.py`
- `tests/test_gemini_client.py`
- `tests/test_gemini_usage_budget.py` (nuevo)

### Fuentes técnicas

Se conservaron las validaciones locales como autoridad. Gemini recibe el
esquema nativo una sola vez y el consumo de entrada informado se usa para
reconciliar reservas. Referencias oficiales consultadas:

- [Structured outputs](https://ai.google.dev/gemini-api/docs/structured-output)
- [Understand and count tokens](https://ai.google.dev/gemini-api/docs/tokens)

La conformidad final corresponde al usuario después de revisar esta entrega.

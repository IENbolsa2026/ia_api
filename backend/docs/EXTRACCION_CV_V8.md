# Extracción de competencias de CV: investigación y ajuste v8

## Problema observado

Un CV con «soy programador en Python» puede llegar a la pantalla sin Python como
habilidad. La extracción v7 separaba bien los títulos y cursos, pero su prompt
podía descartar toda una frase por contener un cargo. Eso afectaba la cobertura
de conocimientos técnicos explícitos. La ausencia de nivel conocido tampoco
debe impedir conservar la habilidad: se usa `unknown`.

La auditoría de la implementación encontró tres vías de pérdida adicionales:

1. El manifiesto podía cortar un nombre entre paquetes cuando un texto largo no
   tenía saltos de línea. Ninguna petición veía el nombre completo.
2. La regla de curso aceptaba solo unos pocos verbos de práctica. Descripciones
   como «curso donde desarrollé...» podían rechazarse aunque fueran verificables.
3. Una agrupación propuesta por el modelo podía fusionar Python y Java si ambos
   llevaban un `concept_id` demasiado genérico. La primera etiqueta sobrevivía y
   la otra desaparecía.

Estos defectos son verificables sin CV privados. No hay evidencia de que cada
omisión real se deba a uno de ellos; hay que volver a analizar los CV afectados
para medir el resultado concreto.

## Criterio de extracción

La unidad es una capacidad atribuida a la persona y respaldada por una cita
literal. El origen de la mención se interpreta por contexto:

| Texto del CV | Hecho que se conserva | Lo que no se presupone |
| --- | --- | --- |
| «Soy programador en Python» | Python, técnico, declarado, nivel desconocido; cargo aparte | Otras tecnologías habituales de programación |
| «Stack: C++, C#, .NET, Node.js, SQL» | Cinco conocimientos técnicos independientes | Un nivel avanzado para todos |
| «Curso de Excel» | Estudio/curso | Excel dominado, certificado obtenido |
| «En el curso desarrollé presupuestos con Excel» | Curso y Excel, con su cita de práctica | Nivel experto |
| «Doctorado en curso; desarrollo scripts en Python» | Doctorado `in_progress` y Python | Título de doctor obtenido |
| «No sé Java; quiero aprender React; uso Python» | Python | Java y React |

La distinción entre conocimiento y ejecución sigue la clasificación oficial
[ESCO](https://esco.ec.europa.eu/en/classification/skill-main): ambos pueden ser
conceptos del pilar de habilidades, mientras ocupaciones y cualificaciones son
entidades diferentes. Los ejemplos de [SkillSpan](https://aclanthology.org/2022.naacl-main.366.pdf)
separan nombres de herramientas y conocimientos (incluidos Python, Excel,
JavaScript, Node.js) de puestos; el trabajo estudia anuncios, por lo que la
adaptación aquí exige que el CV atribuya cada hecho a su titular. La extracción
desde frases complejas se beneficia de ejemplos contextualizados, como estudia
[Nguyen et al.](https://aclanthology.org/2024.nlp4hr-1.3/). La [guía de prompts
de Gemini](https://ai.google.dev/gemini-api/docs/prompting-strategies?hl=en)
recomienda ejemplos consistentes y evaluación iterativa; su [salida estructurada](https://ai.google.dev/gemini-api/docs/structured-output)
asegura forma JSON, pero no garantiza por sí sola cobertura semántica.

Por eso el prompt v8 incluye ejemplos positivos y negativos de varios sectores,
una pasada de inventario de tecnologías/herramientas/procedimientos y una
comprobación final de omisiones. El validador mantiene citas literales y la
taxonomía cerrada de diez habilidades blandas. El nivel se estima por evidencia
de dominio de *esa* habilidad; la mera mención conserva `unknown`.

## Evaluación

`tests/fixtures/cv_extraction_quality.json` tiene 13 CV sintéticos: software,
herramientas de oficina, mecánica, ventas, salud, curso en progreso, doctorado,
negaciones, aspiraciones y habilidades blandas. La herramienta
`tools/evaluate_cv_extraction.py` ejecuta el pipeline real y registra habilidades
faltantes, habilidades prohibidas, estado académico, llamadas, latencia y tokens
reportados por el proveedor. Usa el limitador de cuota configurado. No lee CV de
usuarios ni escribe claves. Ejecutar v7 y v8 con los mismos casos permite comparar
recall y falsos positivos; las etiquetas son una guía de regresión, no una
estimación estadística de precisión para la población.

```powershell
.\.venv\Scripts\python.exe tools\evaluate_cv_extraction.py --prompt v8 --output C:\IEN\revisiones\cv-v8-20261022\evaluacion-v8.json
```

Al implementar v8, la primera llamada de v7 y el reintento v8 recibieron HTTP
503 de Gemini. Ambos reportes quedan como indisponibilidad del proveedor, no como
fallos de extracción. Las pruebas locales del contrato y pipeline son el respaldo
disponible hasta que Gemini vuelva a responder. Repetir la evaluación completa y
una revisión humana de una muestra desidentificada antes de interpretar mejoras
de calidad. Reanalizar un CV previo crea un perfil v8; los perfiles históricos no
se alteran solos.

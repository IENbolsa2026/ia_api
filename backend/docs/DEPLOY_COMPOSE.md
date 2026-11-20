# Desplegar ia_api en Debian con Docker Compose

El Compose de producción arranca FastAPI, un worker Celery y Redis. La API se
publica en `127.0.0.1:8001` para que la web de BolsaTrabajo que corre en el host
pueda usarla. Redis y el worker se quedan en la red de Compose. La API y el
worker comparten un volumen privado para los archivos de CV. Redis conserva su
estado en otro volumen con AOF. El Compose de desarrollo sigue disponible para
quien use el Makefile.

## Configurar

Con Docker Engine y el plugin Compose instalados, entrá en `backend/` y creá
un archivo de entorno privado. Hacé esto una vez por entorno.

```sh
umask 077
cp -n .env.example .env.compose
chmod 600 .env.compose
```

Editá `.env.compose` antes de levantar los servicios:

- Definí `LLM_PROVIDER` y `MATCH_PROVIDER` y completá las claves y modelos
  de los proveedores elegidos. La plantilla base selecciona Groq para ambos.
- Poné un `SERVICE_API_TOKEN` fuerte que coincida con el token configurado en
  la web. Usá comillas simples en `.env.compose` para secretos que contengan
  `$`, por ejemplo `SERVICE_API_TOKEN='valor-privado-con-$-literal'`. Compose
  interpola `$VARIABLE` dentro de valores sin comillas o con comillas dobles:
  puede alterar el token y provocar un 401 aunque los archivos parezcan iguales.
  Completá también los secretos requeridos por la plantilla.
- Cambiá `CV_RESULT_CALLBACK_URL` a la URL HTTPS real del callback de
  BolsaTrabajo, por ejemplo `https://bolsa.example/ia/llegue/`. Desde el worker,
  `127.0.0.1` apunta a su propio contenedor. Alineá
  `CV_RESULT_CALLBACK_HMAC_SECRET` y, si tu web lo usa,
  `CV_RESULT_CALLBACK_SECRET` con los valores de la web.
- Compose fija `CHANNEL_REDIS_URL=redis://redis:6379/3`, junto con las demás
  URLs de Redis, `MODE` y el directorio de artefactos. Ya no hereda el
  `localhost` de desarrollo: dentro del worker apuntaría al propio contenedor.
  Si usabas un Redis externo para avisos, trasladá ese valor a
  `DOCKER_CHANNEL_REDIS_URL` en `.env.compose`; esta es ahora la variable de
  override del Compose de producción. `CHANNEL_REDIS_URL` sigue siendo la
  variable de ejecución de la aplicación y del desarrollo sin Docker.
- Mantené vacíos `CV_LEGACY_CONVERTER_IMAGE` y `SKILL_CATALOG_DATABASE_URL`
  para el despliegue base. Este perfil cubre PDF, DOCX, ODT y TXT. El conversor
  de DOC/RTF y el catálogo semántico PostgreSQL/pgvector son integraciones
  opcionales con infraestructura propia.

Si Django en el host tiene que usar el Redis de este Compose para los avisos,
creá un archivo local `redis-loopback.yml` con lo siguiente y agregá
`-f redis-loopback.yml` después del archivo principal en cada comando. Usá
`redis://127.0.0.1:6380/3` en Django y mantené `redis://redis:6379/3` en la API.

```yaml
services:
  redis:
    ports:
      - "127.0.0.1:6380:6379"
```

## Conectar BolsaTrabajo

En el entorno de Django y sus despachadores, las URLs apuntan al puerto
publicado en el host. Ajustalas si la web ya tiene otra configuración.

```dotenv
IA_API_URL=http://127.0.0.1:8001/api/v1/cv-analyses
IA_OFFER_API_URL=http://127.0.0.1:8001/api/v1/offer-analyses
MATCH_API_URL=ws://127.0.0.1:8001/api/v1/ws/v1/matches
MATCH_API_HTTP_URL=http://127.0.0.1:8001/api/v1
```

Igualá `IA_API_TOKEN` y `MATCH_API_TOKEN` al `SERVICE_API_TOKEN` de la API.
Igualá `IA_CALLBACK_HMAC_SECRET` a `CV_RESULT_CALLBACK_HMAC_SECRET`, y
`IA_CALLBACK_TOKEN` a `CV_RESULT_CALLBACK_SECRET` si está configurado.
La ruta HTTPS del callback debe aceptar las solicitudes del worker a través de
Nginx. Recargá los procesos de Django y los despachadores cuando cambies su
entorno.

## Arrancar y verificar

```sh
docker compose --env-file .env.compose -f docker-compose.prod.yml config --quiet
docker compose --env-file .env.compose -f docker-compose.prod.yml up -d --build --wait
docker compose --env-file .env.compose -f docker-compose.prod.yml ps
docker compose --env-file .env.compose -f docker-compose.prod.yml logs --tail=100 api worker
curl --fail http://127.0.0.1:8001/
```

Si el puerto está ocupado, definí `IA_API_PORT` en `.env.compose` y usá ese
puerto también en Django. La comprobación HTTP confirma que FastAPI responde.
Confirmá en los logs que Celery quedó listo y probá un CV ficticio desde
BolsaTrabajo hasta que la web guarde el resultado del callback. Probá también
un match y el aviso en tiempo real si lo usás.

Para detener los servicios conservando los volúmenes, usá `down` sin `-v`.
Protegé y respaldá los volúmenes según la política de datos del proyecto.

## Extracción con Gemini

Seleccionar `LLM_PROVIDER=gemini` cambia la extracción de CV y ofertas;
`MATCH_PROVIDER` selecciona el proveedor de compatibilidad por separado.
`CV_MODEL` corresponde a Groq; Gemini usa `GEMINI_MODEL`.

Para el perfil de Gemini de la plantilla, configurá explícitamente:

```dotenv
LLM_PROVIDER=gemini
GEMINI_API_KEY='clave-privada'
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_MAX_OUTPUT_TOKENS=16384
CV_EXTRACTION_MAX_TOKENS=16384
CV_PACKET_CHARACTERS=20000
CV_BLOQUE_CARACTERES=20000
GEMINI_MAX_INPUT_TOKENS_PER_REQUEST=20000
GEMINI_INPUT_TOKENS_PER_MINUTE=20000
```

Los valores de presupuesto son controles locales, no amplían las cuotas de
Google. Ajustalos a tu cuenta. Las instrucciones, el esquema, las citas y el
contexto RAG consumen entrada aun para un CV de una hoja. El límite de 12000
que traía la plantilla podía ser menor que esas instrucciones solas.

El tamaño de paquete se mide en caracteres, no en páginas. Un tamaño inicial
mayor reduce llamadas repetidas; el procesamiento divide el texto cuando el
presupuesto de entrada o una respuesta truncada lo requieren. Conserva los
paquetes ya completados. Si las instrucciones solas exceden la entrada,
falla una vez con el presupuesto requerido: no divide indefinidamente ni
lo presenta como límite de salida. El máximo efectivo de salida es el menor
entre `CV_EXTRACTION_MAX_TOKENS` y `GEMINI_MAX_OUTPUT_TOKENS`.

Después de cambiar `.env.compose`, recreá API y worker. Un `restart` por sí
solo no actualiza el entorno del contenedor:

```sh
docker compose --env-file .env.compose -f docker-compose.prod.yml up -d --build --wait api worker
docker compose --env-file .env.compose -f docker-compose.prod.yml logs -f --tail=100 api worker
```

## Distinguir los fallos observados

- `POST /api/v1/cv-analyses` con 202 significa aceptado; no confirma extracción
  ni entrega. `GET /` con 200 solo confirma la comprobación de salud.
- Un 429 es una cuota del proveedor. Se respeta el plazo de reintento; borrar
  análisis o reiniciar Redis no restablece esa cuota.
- `CV packet completed` confirma un paquete. Los hijos de una división
  conservan el índice del padre, de modo que el índice no es un total de llamadas.
- Una validación final local fallida se entrega como error sin repetir la IA
  sobre los mismos paquetes ya validados.
- El callback HTTP confirma el guardado en Django; el aviso de Channels se
  publica después y es independiente. Su Redis debe ser compartido con Django.
- Un trabajo de Celery con `status: missing` corresponde a un mensaje cuyo
  registro fue retirado; se descarta sin llamar al proveedor.

No borres Redis completo ni todos los volúmenes para repetir una prueba: allí
también están las otras colas y resultados. Las pruebas nuevas deben usar un
análisis y una clave de idempotencia nuevos. La API no puede reparar los
eventos de salida de Django que sigan apuntando a trabajos borrados desde su
administrador. Esa corrección, y la supervisión permanente de su dispatcher,
quedan del lado de BolsaTrabajo.

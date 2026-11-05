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
  la web. Completá también los secretos requeridos por la plantilla.
- Cambiá `CV_RESULT_CALLBACK_URL` a la URL HTTPS real del callback de
  BolsaTrabajo, por ejemplo `https://bolsa.example/ia/llegue/`. Desde el worker,
  `127.0.0.1` apunta a su propio contenedor. Alineá
  `CV_RESULT_CALLBACK_HMAC_SECRET` y, si tu web lo usa,
  `CV_RESULT_CALLBACK_SECRET` con los valores de la web.
- Dejá `CHANNEL_REDIS_URL=redis://redis:6379/3` para usar el Redis de Compose,
  o apuntalo al Redis compartido accesible desde el worker. La plantilla de
  desarrollo trae `localhost` y hay que cambiarlo. Compose fija internamente
  las demás URLs de Redis, `MODE` y el directorio de artefactos.
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

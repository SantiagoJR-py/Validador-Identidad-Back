# Identity Document Scanner — MVP 0.1

**Estado:** MVP integrado (FastAPI + frontend web liviano). Lee imágenes y extrae OCR; **NO valida la identidad de una persona ni autentica la cédula**.

## Alcance implementado

- Captura de frente y reverso con `getUserMedia` desde la cámara y `canvas` (sin selector de archivos del navegador).
- Evaluación de calidad: iluminación, desenfoque, resolución y posibles brillos; corrección de perspectiva cuando se detecta un cuadrilátero.
- OCR local con PaddleOCR 3.x; Tesseract es un fallback **de desarrollo** si PaddleOCR no está disponible.
- Extracción conservadora de número candidato, nombres candidatos y fechas candidatas; campos dudosos requieren revisión. **No atribuir precisión de producción.**
- Embed vía iframe con `Content-Security-Policy: frame-ancestors` restringido a orígenes configurados; CORS también restrictivo.
- Sesiones cortas firmadas con HMAC vinculadas al origen exacto del host; API key sólo en backend anfitrión.
- Eventos de progreso y finalización vía `window.postMessage` con `targetOrigin` exacto; servidor host puede consultar eventos y resultado vía API privada.
- Base de datos MySQL/MariaDB (`IDS_DATABASE_URL`): `events` (bitácora sin número de documento ni texto OCR), `results` (copia cifrada temporal, TTL 15 min) e `identity_records` (un registro por persona con datos extraídos y JSON completo).

## Ejecutar localmente

Requiere Python 3.11 recomendado, Tesseract opcional, y acceso a los pesos de PaddleOCR previamente descargados para ejecución offline. `pip install` y la primera inicialización de modelos pueden requerir acceso a internet en una máquina de desarrollo.

```bash
cd identity-document-scanner
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp backend/.env.example .env
# EDITA .env y coloca dos secretos independientes, aleatorios, largos.
set -a; source .env; set +a
python -m uvicorn backend.app.main:app
```

En otra terminal:

```bash
set -a; source .env; set +a
python examples/host_server.py
```

Abre `http://localhost:8080`. El host crea una sesión desde su backend, muestra el iframe y consume los logs. Para usar Docker: `docker compose up --build` (instalar PaddleOCR y descargar pesos exige recursos; no es una imagen ligera). En producción, usa HTTPS en todos los orígenes.

Antes de arrancar, crea la base y el usuario con `sudo mysql < backend/setup_mysql.sql` (cambia `CHANGE_ME`) y ajusta `IDS_DATABASE_URL` en `.env`; las tablas se crean al arrancar. Puede correr junto al Back-End del proyecto con el Validador en `127.0.0.1:8000` y el Back-End en `127.0.0.1:3000`. El Validador tiene sus propios secretos de integración.

**No utilices `file://` ni abras `/embed/` directamente:** se debe obtener un token de sesión desde el backend anfitrión.

## Ejemplo de integración con otra app

1. El backend anfitrión realiza `POST /api/v1/host/sessions` con header `X-API-Key` y JSON `{"parent_origin":"https://app.example.com"}`. El origen debe figurar exactamente en `IDS_ALLOWED_ORIGINS`.
2. El backend anfitrión devuelve al frontend sólo `embed_path` y `session_id`, nunca su API key. El token viaja en el fragmento de URL, que no se envía al servidor web. El navegador renderiza `<iframe src="https://ids.example.com{embed_path}" allow="camera"></iframe>`.
3. En el frontend anfitrión, escucha `message` validando **`event.origin === "https://ids.example.com"`**, `event.source === iframe.contentWindow`, `event.data.source === "IDS"` y el `sessionId` esperado.
4. Tipos de mensaje: `IDS_READY`, `IDS_CAPTURE_STARTED`, `IDS_SIDE_PROCESSED`, `IDS_ERROR`, `IDS_COMPLETE`. `IDS_COMPLETE` contiene datos extraídos, que son sensibles: manejarlos con autorización y sin imprimirlos en consola.
5. Para logs de backend usa `GET /api/v1/host/sessions/{session_id}/events?after_id=0`, autenticado con `X-API-Key` **desde el servidor anfitrión**; para resultado usa `GET /api/v1/host/sessions/{session_id}/result`. Nunca expongas esta clave al navegador.

## API

Documentación Swagger: `http://localhost:8000/docs` (documentación informativa; no exponer en producción sin protección).

| Método | Ruta | Autenticación |
|---|---|---|
| GET | `/api/v1/health` | Pública |
| POST | `/api/v1/host/sessions` | `X-API-Key` |
| GET | `/api/v1/host/sessions/{id}/events` | `X-API-Key` |
| GET | `/api/v1/host/sessions/{id}/result` | `X-API-Key` |
| GET | `/api/v1/embed/session` | Bearer sesión |
| POST | `/api/v1/embed/quality` | Bearer sesión + multipart image |
| POST | `/api/v1/embed/extract?side=front|back` | Bearer sesión + multipart image |
| POST | `/api/v1/embed/finish` | Bearer sesión |
| POST | `/api/v1/embed/events` | Bearer sesión |

## Pruebas

```bash
pytest -q backend/tests
```

## Limitaciones y seguridad antes de producción

- El frontend incluido es HTML/CSS/JS sin paquetes, para ser ejecutable directamente. Se puede reemplazar por Angular sin cambiar el contrato API. **No es Angular todavía.**
- El OCR extrae candidatos, no garantiza la asociación exacta de nombres y fechas en ambas versiones de cédula. Se requieren plantillas por formato, métricas con datos autorizados y revisión manual.
- Una app web no puede garantizar que la cámara observa un documento físico genuino; negar importación desde galería **no** garantiza procedencia de imagen.
- CSP y CORS **no** reemplazan autenticación. Los orígenes autorizados se configuran en servidor; el navegador no puede añadirlos libremente.
- Habilitar rate limits, autorización específica por cliente/tenant, anti-replay, revocación de sesión, políticas de expiración y purga programada de eventos/resultados, auditoría, TLS y robustecimiento antes de procesar documentos reales.
- SQLite cifrado para resultados no equivale a cifrado completo de base de datos. Gestionar la clave fuera del repositorio; rotarla y gestionar copias de seguridad conforme a la política de privacidad.
- Las advertencias de calidad no bloquean la captura en este prototipo; el usuario puede repetir o continuar, pero no debe etiquetarse como extracción confiable sin revisión.
- No se utilizó ningún servicio externo de IA ni la API key compartida por el usuario. Si se expuso una clave en conversación, revocarla y rotarla.

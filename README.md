# PERMUTADOS

Marketplace de compra/venta/permuta que funciona **dentro de WhatsApp**. Un bot 1:1 (WhatsApp Cloud API)
conversa con el usuario mediante un LLM local (**Dolphin-Mistral-24B-Venice-Edition**, servido con una API
compatible con OpenAI). El backend es la única fuente de verdad: Dolphin solo puede *pedir* acciones a través
de una lista cerrada de herramientas; los permisos, estados, pagos y la liberación de contactos los decide el
backend. Una **consola Android nativa** permite administrar todo usando solo la API admin del backend.

> Repositorio privado. Todos los derechos reservados (ver `LICENSE`).

## Estado: Fase 4 completa (consola Android + cierre)

| Área | Estado |
|---|---|
| Modelo de datos (23 tablas, enums, constraints, triggers, índices parciales, FTS español) | ✅ Fase 1 |
| Migraciones Alembic `0001` + `0002` + `0003` (upgrade / downgrade con datos / `alembic check` sin drift) | ✅ |
| Publicaciones, imágenes, búsqueda, ofertas, pagos, liberación, anti-evasión, Admin API, modos, canal manual | ✅ Fase 1 |
| **Motor de conversación**: webhook WhatsApp → cola durable → worker → intención → máquina de estados → Dolphin → herramientas | ✅ Fase 2 |
| **Cliente WhatsApp Cloud API**: texto, imagen, plantilla, marcar leído, descarga de media, reintentos, idempotencia | ✅ Fase 2 |
| **Ventana de 24h**: sesión gratis dentro de la ventana; fuera, una plantilla aprobada y el resto espera | ✅ Fase 2 |
| **Outbox** despachado por el worker (at-least-once, backoff, `SKIP LOCKED`, estados de entrega de Meta) | ✅ Fase 2 |
| **Mercado Pago Checkout Pro** (preferencia ARS 3000, webhook `payment` + `merchant_order`, validaciones) | ✅ Fase 2 |
| **Cifrado a nivel campo** de teléfonos, nombres y mensajes sensibles (AES-256-GCM + índice ciego) | ✅ Fase 2 |
| **Workers recuperables**, outbox sin duplicados, bus de eventos, canal manual (puntaje, diversidad, exposición, reconciliación), modos, backups, métricas | ✅ Fase 3 |
| **Consola Android** (Kotlin + Compose, MVVM/Clean, Retrofit/OkHttp, sesión cifrada con Keystore, solo HTTPS), 12 secciones conectadas a las 49 operaciones admin | ✅ Fase 4 |
| Endpoints admin que faltaban para la consola (detalle/edición de publicaciones, operaciones, conversaciones auditadas, advertencias, auditoría) | ✅ Fase 4 |
| **E2E obligatorio** (flujo completo + 13 ramas de la sección 50) | ✅ Fase 4 |
| Pruebas con credenciales reales/sandbox de Meta y Mercado Pago | ⏳ requiere credenciales del dueño |
| Servidor Dolphin físico | ⏳ cuando exista el servidor |
| Tests instrumentados Android en dispositivo real, APK de release firmado | ⏳ requiere el equipo y el keystore del dueño |

Resultado de la última corrida (2026-10-08, hora de Argentina): **backend 357 tests OK, 0 fallidos** (PostgreSQL 17
y Redis reales); **Android**: `assembleDebug`, `assembleRelease` (sin firmar) y `assembleDebugAndroidTest` OK,
**45/45 tests unitarios** (JVM + Robolectric) OK, lint sin errores; tests instrumentados **no ejecutados** (ver
[Consola Android](#consola-android-de-administración-fase-4)). Docker **no se pudo ejecutar** en la máquina de
construcción (no está instalado): `docker-compose.yml` y el Dockerfile están escritos pero no probados.

## Índice

[Arquitectura](#arquitectura) · [Reglas de negocio](#reglas-de-negocio-clave) · [Seguridad de la IA](#seguridad-de-la-ia) ·
[Instalación](#instalación) · [PostgreSQL](#postgresql) · [Redis](#redis) · [Credenciales y .env](#credenciales-y-env) ·
[Migraciones](#migraciones) · [Docker](#docker) · [Dolphin](#dolphin) · [WhatsApp](#whatsapp-cloud-api) ·
[Mercado Pago](#mercado-pago) · [Webhooks](#webhooks) · [Cifrado](#cifrado-de-datos-personales-fase-2) ·
[Workers y scheduler](#workers-y-recuperación-fase-3) · [Canal](#canal-de-whatsapp-manual-fase-3) ·
[Métricas](#métricas-sección-48) · [Consola Android y APK](#consola-android-de-administración-fase-4) ·
[Backups y recuperación](#backups-y-recuperación-ante-desastres-sección-45) · [Producción](#producción) ·
[Tests](#tests) · [E2E](#e2e-obligatorio-sección-50) · [Real vs. simulado](#qué-es-real-y-qué-es-simulado) ·
[Definition of Done](#definition-of-done-sección-55) · [Troubleshooting](#troubleshooting) · [Pendiente del dueño](#pendiente-del-dueño-antes-de-producción) ·
[Limitaciones](#limitaciones-conocidas)

## Arquitectura

```
WhatsApp Cloud API ──► /webhooks/whatsapp (firma HMAC, dedupe, cifra la identidad, responde 200 rápido)
                              │  webhook_events (cola durable, sobre cifrado)        estados ──► notifications
                              ▼
               Worker: process_inbound_queue (un consumidor, orden por usuario)
                 descarga de media ─► conversation.handle_inbound
                      dedupe → reabre ventana 24h → estado de cuenta → rate limit → anti-evasión
                      → validación de imagen → reconcile(estado) + detect_intent ─► Dolphin (propone)
                                                                       │ tool calls (allowlist)
                                                                       ▼
                         domain services ◄── tools.execute (valida, decide)  ─► state_machine.apply_tool_result
                                │                       └─► domain_events ─► handlers (canal, auditoría)
                                ▼
                    notifications (outbox) ─► Worker: dispatch_outbox ─► WhatsAppClient (texto / plantilla)
Mercado Pago ──► /webhooks/mercadopago (firma) ─► consulta API ─► valida ─► liberación ─► outbox (cifrado)
Scheduler (mismo proceso worker): vencimientos, reconciliación de pagos y del canal, recuperación, retención
PostgreSQL = fuente de verdad · Redis = rate limiting
Consola Android ──HTTPS + JWT──► /admin/* (RBAC VIEWER < MODERATOR < ADMIN < SUPERADMIN, auditado)
```

```
backend/            FastAPI + SQLAlchemy 2 + Alembic (Python 3.13)
  app/core/         config, db, seguridad (argon2id, JWT), rate limit, logging con redacción de secretos
  app/models/       modelo de datos
  app/services/     lógica de dominio (cada módulo es independiente y testeable)
  app/api/          rutas HTTP (health, webhooks, admin, admin/console)
  app/workers/      runtime recuperable (jobs, heartbeats, recuperación) + scheduler
  migrations/       Alembic
  tests/            unit / integration / security / e2e
android/admin-console/  consola Android (Kotlin, Compose, Gradle con version catalog) — ver su README
dolphin/            prompts, políticas y definición de herramientas (montado como volumen)
docker/             Dockerfile del backend, nginx de referencia
scripts/            create_admin, backup/restore de PostgreSQL, rotación de clave, tests, export de herramientas
admin-web/opcional/ nota: el panel web es opcional y no está implementado (la consola es la app Android)
```

## Reglas de negocio clave

* **Fee:** ARS 3000 por liberación de contacto (`CONTACT_RELEASE_PRICE`, editable en `system_config` vía
  `PUT /admin/config`). Solo se acepta ARS. El monto se congela en la operación al aceptar la oferta.
* **Ofertas — 24h, comunicadas a ambas partes desde el inicio.** Al crear una oferta:
  * El ofertante recibe la confirmación: **"⏳ Tu oferta vence en 24h (el …)"**.
  * El dueño recibe la notificación: **"⏳ Tenés 24h para aceptar o rechazar, luego la oferta caduca."**
  * Las horas salen de `offer_expiration_hours` (por defecto 24) y la fecha/hora límite se muestra en hora de
    Argentina. Los textos viven en `backend/app/services/notifications/templates.py`
    (`offer_created_bidder`, `offer_received_owner`) y se encolan en el mismo commit que crea la oferta.
    Tests: `tests/unit/test_templates.py`, `tests/integration/test_offers.py`, `tests/e2e/test_e2e_flow.py`.
* Mientras una oferta está pendiente, el artículo queda **pausado** (estado `RESERVED`: oculto en búsquedas y sin
  nuevas ofertas). Vuelve a `PUBLISHED` si la oferta se rechaza, vence o se cancela.
* Al aceptar, el ofertante tiene **24h para pagar**; si no paga, la oferta se cancela y el artículo vuelve a
  `PUBLISHED`. El dueño puede **arrepentirse** después de aceptar, pero solo hasta que el pago esté aprobado
  (antes de permitirlo, el backend consulta a Mercado Pago).
* Un ofertante puede tener como máximo 3 ofertas activas (configurable), porque cada oferta pausa un artículo.
* **Pago confirmado solo por Mercado Pago** (webhook firmado + consulta a la API). "Ya pagué" nunca cambia nada.
  Pagos aprobados fuera de término, duplicados o con monto/moneda incorrectos quedan en `NEEDS_REFUND` y no
  liberan datos.
* **Liberación:** ambos reciben el WhatsApp del otro y la misma palabra clave única aleatoria (p. ej.
  `LUNA-7428`) para reconocerse. El artículo pasa a `COMPLETED`.
* **Anti-evasión** (teléfonos, emails, links, redes, frases tipo "pasame tu número", números escritos en
  letras): 1er intento → advertencia que dice explícitamente que el próximo intento será penalizado; 2do intento →
  cuenta `SUSPENDED` + ban `PENDING_REVIEW`; un admin decide (`/ban` o `/reinstate`). El contador vive en el
  backend y no se pierde aunque la operación bloqueada se revierta.
* **Canal de WhatsApp: manual.** La Cloud API oficial no puede publicar en Canales. El backend calcula un puntaje
  de atractivo y entrega una lista **sugerida, ordenada y diversificada**; el admin publica a mano y registra lo que
  hizo. Cuando un artículo se cierra (completado, vencido o borrado) o su dueño es suspendido, su post queda al
  instante en `REMOVAL_PENDING` y aparece en la lista de bajas. Ver [Canal de WhatsApp manual](#canal-de-whatsapp-manual-fase-3).
* **Modos:** `PAUSED` bloquea nuevas ofertas, aceptaciones y links de pago, y difiere las liberaciones (publicar y
  buscar siguen funcionando); `EMERGENCY` además bloquea publicar y editar y retiene los mensajes con datos de
  contacto. Los pagos que llegan por webhook se registran siempre y la liberación se completa al volver a `ONLINE`.
  Mientras el sistema no está `ONLINE` los **plazos se congelan** (nadie puede aceptar ni pagar): al volver, las
  ofertas pendientes y las ventanas de pago se extienden por el tiempo pausado y se avisa a los afectados.

## Seguridad de la IA

* Dolphin solo ve un contexto mínimo (sin teléfonos, sin secretos) y puede llamar exactamente 20 herramientas
  (`dolphin/tools/tools.json`, generado con `scripts/export_dolphin_tools.py`). Cualquier otra (SQL, shell,
  archivos, "marcar pagado", etc.) se rechaza y genera un evento de seguridad.
* Los argumentos se validan con Pydantic (`extra=forbid`): no se puede inyectar `user_id`, `status` ni montos.
  La identidad sale siempre de la sesión de WhatsApp.
* El texto del usuario va siempre en rol `user`; los prompts de sistema no se mezclan con datos del usuario.
* La salida del modelo pasa por un filtro (datos de contacto, secretos).
* Si Dolphin no responde, el bot contesta un mensaje seguro y no cambia ningún estado.
* **Dolphin propone, el backend decide.** La intención se detecta de forma determinística
  (`conversation/intents.py`) y el estado de la conversación vive en una máquina de estados del backend
  (`conversation/state_machine.py`): el estado solo avanza después de una herramienta exitosa, las transiciones
  ilegales se rechazan (p. ej. `IDLE → PUBLISHING_PREVIEW`) y en cada turno `reconcile` re-deriva el estado desde
  la base (borrador borrado, oferta vencida, pago pendiente…). Dolphin recibe `conversation_state`,
  `current_intent` y `expected_next` como **datos** en `BACKEND_CONTEXT`.
* **Mensajes del backend vs. texto del modelo.** El link de pago de Mercado Pago lo envía el backend como mensaje
  propio (canal `backend_messages`), sin pasar por el modelo ni por el filtro; el texto de Dolphin sigue
  filtrado (un link escrito por el modelo se bloquea).

## Instalación

Requisitos: Python 3.13, PostgreSQL 17, Redis 7+ (o Docker, ver [Docker](#docker)). Para la consola: JDK 17 y
Android SDK (ver [Consola Android](#consola-android-de-administración-fase-4)).

```bash
git clone https://github.com/L0B091/permutados.git && cd permutados
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt   # producción: requirements.txt
cp .env.example .env    # completar (ver "Credenciales y .env"); ENCRYPTION_KEY es obligatoria
cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/python ../scripts/create_admin.py admin SUPERADMIN   # pide la contraseña (no va en la línea de comandos)
../.venv/bin/uvicorn --factory app.main:create_app --reload       # API en http://127.0.0.1:8000
../.venv/bin/python -m app.workers.scheduler                      # worker + scheduler (otra terminal)
```

`GET /health` (vivo) y `GET /health/ready` (PostgreSQL + Redis) sirven para comprobar el arranque. Roles de
`create_admin.py`: `VIEWER`, `MODERATOR`, `ADMIN`, `SUPERADMIN`.

### PostgreSQL

PostgreSQL 17 (también funciona 15+; se usan FTS en español, índices parciales, triggers y advisory locks).

```bash
sudo -u postgres psql -c "CREATE ROLE permutados LOGIN PASSWORD '<contraseña>'"
sudo -u postgres createdb -O permutados permutados
sudo -u postgres createdb -O permutados permutados_test      # solo para correr tests (se borra en cada corrida)
# .env:  DATABASE_URL=postgresql+psycopg://permutados:<contraseña>@localhost:5432/permutados
```

`audit_logs` es append-only (trigger). No dar al usuario de la app permisos de superusuario.

### Redis

Redis 7+ para el rate limiting (mensajes por usuario, login admin). `REDIS_URL=redis://localhost:6379/0`. Con
Docker se usa `redis:8-alpine` con AOF. Si Redis se cae, `/health/ready` lo informa y la consola lo muestra en
**Sistema**.

### Credenciales y .env

`.env.example` documenta **todas** las variables (un test lo verifica: `tests/unit/test_env_example.py`) y no
contiene ningún secreto. `.env` está en `.gitignore`: **nunca** se versiona. Secretos necesarios:

| Variable | De dónde sale |
|---|---|
| `ENCRYPTION_KEY` | generar: `python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('='))"` — **guardar copia fuera del servidor y de los backups** |
| `ADMIN_AUTH_SECRET` | generar: `python -c "import secrets;print(secrets.token_urlsafe(48))"` (≥ 32 caracteres) |
| `POSTGRES_PASSWORD` / `DATABASE_URL` | la contraseña elegida para PostgreSQL |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_APP_SECRET`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_BUSINESS_ACCOUNT_ID` | Meta for Developers (ver [WhatsApp](#whatsapp-cloud-api)) |
| `WHATSAPP_VERIFY_TOKEN` | lo inventás vos; el mismo valor va en la configuración del webhook en Meta |
| `MERCADOPAGO_ACCESS_TOKEN`, `MERCADOPAGO_PUBLIC_KEY`, `MERCADOPAGO_WEBHOOK_SECRET` | Mercado Pago Developers (ver [Mercado Pago](#mercado-pago)) |
| `DOLPHIN_BASE_URL`, `DOLPHIN_API_KEY` | tu servidor Dolphin (vacío hasta que exista) |

El backend **se niega a arrancar** en `ENVIRONMENT=production` con configuración insegura (secretos faltantes,
`CORS_ALLOWED_ORIGINS=*`, pasarela de pagos simulada). Los logs redactan tokens, teléfonos y secretos.
La consola Android **no** usa `.env`: solo necesita la URL pública HTTPS del backend y un usuario admin.

### Migraciones

```bash
cd backend
../.venv/bin/alembic upgrade head        # aplicar (el contenedor backend lo hace solo al arrancar)
../.venv/bin/alembic current             # revisión actual (0003)
../.venv/bin/alembic downgrade -1        # volver una (0002 descifra / 0003 revierte con datos)
../.venv/bin/alembic check               # el modelo y la base coinciden (sin drift)
../.venv/bin/alembic revision --autogenerate -m "..."   # nueva migración; revisarla a mano siempre
```

`0001` esquema inicial · `0002` cifrado de datos personales y mensajería (cifra los datos existentes) · `0003`
workers, eventos, canal. Hacer backup antes de migrar en producción. Los tests aplican upgrade → downgrade →
upgrade al inicio de cada corrida.

## Docker

```bash
cp .env.example .env    # ENVIRONMENT=production, secretos reales, POSTGRES_PASSWORD, DATABASE_URL con host "postgres"
docker compose up -d --build              # postgres, redis, backend (corre alembic upgrade head) y worker
docker compose exec backend python /srv/scripts/create_admin.py admin SUPERADMIN
docker compose ps && docker compose logs -f backend worker
```

Servicios: `postgres` (17-alpine, volumen `pgdata`), `redis` (8-alpine, AOF), `backend` (uvicorn, solo en
`127.0.0.1:8000`, healthcheck `/health/ready`), `worker` (`python -m app.workers.scheduler`). El backend corre como
usuario sin privilegios. Exponerlo con un reverse proxy TLS (referencia: `docker/nginx.conf`).

> ⚠️ **No probado**: en la máquina de construcción no hay Docker. Los archivos están escritos y revisados, pero
> `docker compose up` nunca se ejecutó. Si preferís no usar Docker, la [instalación nativa](#instalación) es la
> que se probó (con systemd para backend y worker).

## Dolphin

Mientras `DOLPHIN_BASE_URL` esté vacío, el bot responde un mensaje seguro de "servicio no disponible" y **no cambia
ningún estado**; publicaciones, pagos y la consola siguen funcionando. Cuando exista el servidor, servir el modelo
con cualquier servidor OpenAI-compatible con *tool calling*, por ejemplo vLLM:

```bash
vllm serve dphn/Dolphin-Mistral-24B-Venice-Edition --enable-auto-tool-choice --tool-call-parser mistral
```

y configurar `DOLPHIN_BASE_URL=http://<servidor>:8000/v1` (red interna; `DOLPHIN_API_KEY` si el servidor la pide).
Prompts y políticas se editan en `dolphin/` sin tocar código; `dolphin/tools/tools.json` se regenera con
`scripts/export_dolphin_tools.py`. La consola muestra el estado de Dolphin en **Sistema**.

## WhatsApp Cloud API

Configuración en Meta (documentación oficial de WhatsApp Business Platform / Cloud API):

1. Crear una app de tipo *Business* en developers.facebook.com y agregar el producto **WhatsApp**.
2. Copiar `Phone number ID` → `WHATSAPP_PHONE_NUMBER_ID` y `WhatsApp Business Account ID` →
   `WHATSAPP_BUSINESS_ACCOUNT_ID`. En *App settings → Basic*, el **App secret** → `WHATSAPP_APP_SECRET`.
3. Crear un **usuario de sistema** en Business Manager con permisos `whatsapp_business_messaging` y
   `whatsapp_business_management` y generar un token permanente → `WHATSAPP_ACCESS_TOKEN`.
4. Webhook: URL `https://<tu-dominio>/webhooks/whatsapp`, *Verify token* = `WHATSAPP_VERIFY_TOKEN`, suscribir el
   campo **messages** (trae mensajes entrantes y estados de entrega).
5. Crear y hacer aprobar la plantilla **`permutados_novedad`** (ver abajo). Sin ella, los mensajes fuera de la
   ventana de 24h quedan esperando (`AWAITING_WINDOW`) hasta que el usuario escriba.

### Mensajería (fase 2)

* **Entrada:** `POST /webhooks/whatsapp` verifica `X-Hub-Signature-256`, deduplica por `message_id`, cifra la
  identidad y el texto en un sobre (`webhook_events`) y responde enseguida. El worker
  (`process_inbound_queue`, cada `INBOUND_POLL_SECONDS`) procesa la cola con un advisory lock (un solo consumidor →
  orden por usuario), descarga la imagen (con reintentos, límite de tamaño y verificación sha256), corre la
  conversación y **borra el sobre** al terminar. Errores transitorios → reintento con backoff; después de
  `INBOUND_MAX_ATTEMPTS` → `FAILED`. Tipos no soportados (audio, video, sticker…) reciben una respuesta fija;
  ubicaciones, un aviso (solo zona general); **tarjetas de contacto cuentan como intento de evasión**.
* **Salida (outbox):** todo mensaje saliente es una fila de `notifications` creada en la misma transacción que el
  cambio de dominio. `dispatch_outbox` (cada `OUTBOX_POLL_SECONDS`) procesa un mensaje a la vez en tres pasos:
  **claim** (transacción corta: `FOR UPDATE SKIP LOCKED`, estado `SENDING`, `sending_started_at`) → **envío** a Meta
  fuera de toda transacción → **settle** (segunda transacción: `SENT` / reintento con backoff / `AWAITING_WINDOW`).
  Ver [Reintentos sin duplicados](#reintentos-de-whatsapp-sin-duplicados).
* **Ventana de 24h:** cada mensaje entrante guarda `users.last_inbound_at`. Dentro de la ventana (con un margen de
  `WHATSAPP_WINDOW_SAFETY_MINUTES`) se manda texto libre (gratis). Fuera de la ventana se manda **una sola**
  plantilla aprobada por período de silencio (máx. 1 cada 24h) con un resumen sin datos personales, y los mensajes
  completos quedan en `AWAITING_WINDOW` hasta que la persona responda; los que tienen vencimiento (avisos de
  oferta/pago) pasan a `EXPIRED` si caducan antes. El error 131047 de Meta (fuera de ventana) se maneja tanto en la
  respuesta como en los webhooks de estado (`sent/delivered/read/failed` se guardan en la notificación).
* **Plantilla requerida en Meta:** crear y aprobar en WhatsApp Manager una plantilla **UTILITY** llamada
  `permutados_novedad` (o la de `WHATSAPP_FALLBACK_TEMPLATE`), idioma **es_AR**, con **una** variable `{{1}}`
  en el cuerpo. Ej.: *"Tenés una novedad en Permutados: {{1}}. Respondé este mensaje para verla."*
* `biz_opaque_callback_data` lleva la clave de idempotencia de la notificación (`<clave>` en mensajes de sesión,
  `<clave>:tpl` en plantillas); Meta la devuelve en los webhooks de estado y así se confirma un envío cuyo
  resultado no llegamos a guardar.
## Mercado Pago

Configuración (Mercado Pago Developers → *Tus integraciones*):

1. Crear una aplicación (Checkout Pro). Credenciales de **prueba** para sandbox y de **producción** después:
   `MERCADOPAGO_ACCESS_TOKEN`, `MERCADOPAGO_PUBLIC_KEY`, `MERCADOPAGO_ENVIRONMENT=sandbox|production`.
2. *Webhooks*: URL `https://<tu-dominio>/webhooks/mercadopago`, eventos **Pagos** (`payment`) y **Órdenes
   comerciales** (`merchant_order`). Copiar la **clave secreta** → `MERCADOPAGO_WEBHOOK_SECRET`.
3. Opcional: `MERCADOPAGO_COLLECTOR_ID` (tu user id; se rechazan pagos a otro cobrador) y `MERCADOPAGO_BACK_URL`.
4. Probar en sandbox con usuarios de prueba (comprador y vendedor) antes de pasar a producción. En producción se
   rechazan pagos sandbox (`live_mode`).

### Pagos (fase 2)

* `create_payment` crea una preferencia **Checkout Pro** real: 1 ítem (categoría `services`), ARS
  `CONTACT_RELEASE_PRICE`, `binary_mode`, sin efectivo/ticket, 1 cuota, `external_reference` único,
  `notification_url`, vencimiento = fin de la ventana de pago, `statement_descriptor`, `X-Idempotency-Key`
  determinística. Sandbox usa `sandbox_init_point`.
* El webhook (firma `x-signature` v1 + tolerancia de tiempo) **nunca confía en el cuerpo**: consulta
  `GET /v1/payments/{id}` (o `GET /merchant_orders/{id}` → pagos) y valida estado, monto, moneda,
  `external_reference`, operación, `live_mode` (en producción se rechazan pagos sandbox) y, si se configura,
  `MERCADOPAGO_COLLECTOR_ID`. Discrepancias → `PAYMENT_MISMATCH` (evento de seguridad + auditoría), sin liberar.
  Procesamiento idempotente (mismo pago dos veces = una liberación).
## Webhooks

| Ruta | Quién llama | Verificación | Qué hace |
|---|---|---|---|
| `GET /webhooks/whatsapp` | Meta (alta del webhook) | `hub.verify_token` = `WHATSAPP_VERIFY_TOKEN` | devuelve `hub.challenge` |
| `POST /webhooks/whatsapp` | Meta | `X-Hub-Signature-256` (HMAC-SHA256 con `WHATSAPP_APP_SECRET` sobre el cuerpo crudo) | encola mensajes cifrados (dedupe por id), registra estados de entrega; responde 200 rápido |
| `POST /webhooks/mercadopago` | Mercado Pago | `x-signature` v1 (`ts` + `v1`, con `x-request-id` y `data.id`) + tolerancia de tiempo | consulta a la API de MP, valida y libera (idempotente) |

Firma inválida → 401 y evento de seguridad. Ambos son idempotentes: un webhook repetido (o con otro request id)
nunca libera dos veces (probado en `tests/e2e/test_e2e_branches.py::test_duplicate_webhooks_release_exactly_once`).
El reverse proxy **no debe modificar el cuerpo** (rompería la firma). Exponer públicamente solo `/webhooks`,
`/admin` y `/health`.

## Cifrado de datos personales (fase 2)

* `ENCRYPTION_KEY` (32 bytes, base64 urlsafe) es **obligatoria**. AES-256-GCM con nonce aleatorio, el *propósito*
  como dato asociado (un cifrado de teléfono no sirve como nombre), claves derivadas con HKDF y formato versionado
  `v1.<kid>.<datos>`.
* Cifrado: `users.whatsapp_enc`, `users.display_name_enc`, la evidencia de mensajes bloqueados por anti-evasión
  (`messages.body_enc`) y los mensajes de liberación (`notifications.body_enc`), además del sobre de la cola de
  entrada. Para buscar usuarios se usa un **índice ciego** HMAC-SHA256 (`users.whatsapp_hash`); se guardan los
  últimos 4 dígitos para mostrar `•••1234`.
* **Descifrado solo en `app/services/users/pii.py`** (un test de arquitectura AST lo impone): al liberar el contacto,
  en memoria al entregar el mensaje por WhatsApp, y en vistas de admin auditadas de un solo usuario (nombre y
  evidencia de evasión, necesarias para revisar suspensiones). Los listados de admin muestran el número enmascarado
  y la búsqueda por número es exacta (vía hash).
* Migración `0002` cifra los datos existentes y elimina las columnas en texto plano (el downgrade los descifra).
* Rotación: `ENCRYPTION_KEY=<nueva>`, `ENCRYPTION_OLD_KEYS=<vieja>`, worker detenido, y
  `python ../scripts/rotate_encryption_key.py` (re-cifra y re-indexa en una transacción).
* **Perder la clave = perder los teléfonos.** Guardarla fuera de los backups de la base.

## Workers y recuperación (fase 3)

`python -m app.workers.scheduler` (o el servicio `worker` de docker compose). Secuencia de arranque
(secciones 23, 31, 32, 47 — *recover jobs → reconcile → continue*):

1. registra un **heartbeat** (`worker_heartbeats`, cada 15 s; un worker sin latido por 60 s se considera caído);
2. `recover_on_startup`: los jobs que quedaron `RUNNING` de un worker muerto pasan a `ABANDONED` (con fila en
   `job_runs`), los mensajes que quedaron `SENDING` se recuperan, los handlers de eventos fallidos se reintentan y
   todo se audita (`WORKER_RECOVERY`). Si otra réplica viva está corriendo un job, no se toca;
3. corre **una vez** cada job de mantenimiento (pone al día lo que venció durante la caída);
4. agenda todo. Cada job es idempotente, respeta el modo del sistema y toma un `pg_try_advisory_xact_lock`
   (varias réplicas o un reinicio a mitad de una corrida son seguros). La función corre en una transacción: si falla,
   no queda nada a medias y se registra `FAILED` con `consecutive_failures`.

`job_state` guarda por job: último inicio/fin/éxito, duración, filas afectadas, último error, fallos consecutivos y el
último resultado (p. ej. el informe de reconciliación del canal). `job_runs` guarda solo corridas que hicieron algo,
fallaron o se abandonaron (retención 30 días). SIGTERM/SIGINT detienen el scheduler y marcan el heartbeat como
detenido. `GET /admin/workers` y `GET /admin/system/health` muestran workers vivos y estado de cada job.

| Job | Intervalo | Modos en que corre | Qué hace |
|---|---|---|---|
| `expire_pending_offers` | 60 s | solo `ONLINE` | ofertas sin respuesta en 24h → `EXPIRED`, artículo vuelve a `PUBLISHED`, aviso a ambos |
| `expire_payment_windows` | 60 s | solo `ONLINE` | ventana de pago de 24h vencida: **primero consulta a Mercado Pago**; si se pagó durante la caída se libera; si MP no responde se posterga; si el pago está "en proceso" se espera (máx. 72h); si no hay pago → cancela y el artículo vuelve a `PUBLISHED` |
| `release_deferred` | 60 s | solo `ONLINE` | libera contactos pagados mientras el sistema estaba pausado |
| `reconcile_pending_payments` | 300 s | todos | busca en MP pagos aprobados cuyo webhook se perdió (idempotente: un pago = una liberación) |
| `warn_expiring_publications` | 1 h | todos | aviso antes del vencimiento de 30 días |
| `expire_publications` | 300 s | todos | 30 días → `EXPIRED` → evento `ITEM_EXPIRED` → post del canal `REMOVAL_PENDING` → auditoría |
| `channel_reconciliation` | 600 s | todos | compara la base con el estado registrado del canal (ver abajo) |
| `refresh_channel_scores` | 900 s | `ONLINE`, `PAUSED` | recalcula y guarda puntaje + desglose |
| `retry_event_handlers` | 60 s | todos | reintenta handlers best-effort fallidos (backoff 30 s·2ⁿ, máx. 8) |
| `recover_outbox_in_flight` | 300 s | todos | mensajes `SENDING` más viejos que `OUTBOX_SENDING_GRACE_SECONDS` |
| `prune_history` | 24 h | todos | retención de `job_runs`, heartbeats e interacciones (los audit logs nunca se borran) |
| `process_inbound_queue` / `dispatch_outbox` | 2 s | todos | mensajería (fase 2) |

**Modos en los workers (sección 46).** `PAUSED`: no corren los plazos ni las liberaciones; sí webhooks, pagos,
reconciliación, recuperación, vencimiento de publicaciones, bajas del canal y auditoría. `EMERGENCY`: además se
retienen los mensajes con datos de contacto (`sensitive`, se reintentan cada 5 min) y no se recalculan puntajes.
Al volver a `ONLINE` (`PUT /admin/system/mode`) `system.modes.extend_deadlines` corre los vencimientos de ofertas y
de pagos por la duración de la pausa, avisa a dueño/ofertante y audita `SYSTEM_DEADLINES_EXTENDED`; si el link de
pago vencía antes del nuevo plazo, `create_payment` genera uno nuevo (`payments.checkout_expires_at`).

### Reintentos de WhatsApp sin duplicados

* La fila se marca `SENDING` en una transacción **confirmada antes** de llamar a Meta; nunca se mantiene un lock
  durante la llamada HTTP, y otro dispatcher no puede tomarla.
* Si el proceso muere después de que Meta aceptó el mensaje, el webhook de estado trae `biz_opaque_callback_data`
  (nuestra clave) y la fila pasa a `SENT` sin reenviar. Si el settle llega tarde, detecta que ya se resolvió.
* Si no hay evidencia después de `OUTBOX_SENDING_GRACE_SECONDS` (15 min), un mensaje de sesión vuelve a `PENDING`
  y se envía una vez más con la misma clave. Una **plantilla** en duda nunca se reenvía (es paga y el usuario ya
  puede tenerla): queda en `AWAITING_WINDOW`. El cupo de plantilla se reserva en el claim y se devuelve si falla.
* Un segundo `wamid` para la misma clave se registra como `duplicate_message_ids` (visible para el admin).

## Eventos internos (sección 35)

`app/services/events/bus.py`. Cada evento se persiste en `domain_events` **en la misma transacción** que el cambio:
`ITEM_CREATED / UPDATED / STATUS_CHANGED / COMPLETED / EXPIRED / DELETED`, `OFFER_CREATED / ACCEPTED / REJECTED /
EXPIRED / CANCELLED`, `PAYMENT_CONFIRMED`, `CONTACT_RELEASED`, `USER_SUSPENDED`, `USER_BANNED`.

* Handler **crítico** (`critical=True`): corre en la transacción del llamador; si falla, la operación se revierte.
* Handler **best effort** (por defecto): corre dentro de un SAVEPOINT; si falla, la operación sigue, el evento queda
  con `failed_handlers` / `last_error` y el job `retry_event_handlers` lo reintenta. `processed_at` se completa
  solo cuando todos los handlers terminaron bien.
* El canal se suscribe a `ITEM_COMPLETED / EXPIRED / DELETED` y `USER_SUSPENDED / BANNED`: el post queda en
  `REMOVAL_PENDING` **al instante** (no espera al polling). Es best effort a propósito: un error del canal nunca
  bloquea una liberación de contacto; la reconciliación es la red de seguridad.

## Canal de WhatsApp manual (fase 3)

El canal lo administra una persona. El backend **no publica nada**: sugiere, registra y vigila.

**Puntaje de atractivo 0-100** (`app/services/channel/score.py`, sección 26). Cada señal se normaliza a 0..1 y se
pondera (los pesos suman 100):

| Señal | Peso | Señal | Peso |
|---|---:|---|---:|
| demanda de la categoría | 8 | aceptación del dueño (suavizada) | 5 |
| búsquedas (usuarios distintos que lo vieron) | 10 | recencia (14 días) | 10 |
| selecciones | 10 | precio vs. mediana de la categoría | 8 |
| vistas de detalle | 7 | acepta permuta | 4 |
| ofertas (ofertantes distintos) | 10 | calidad de la foto | 10 |
| demanda de la zona | 5 | completitud | 8 |
| tiempo restante antes de vencer | 5 | | |

*Disponibilidad* es una compuerta: si no está `PUBLISHED`, venció, tiene un reporte abierto o el dueño no está
`ACTIVE`, el puntaje es 0 y nunca se sugiere. **Anti-abuso:** las interacciones se cuentan como *usuarios distintos
por día* (`item_interactions`; un usuario que busca 50 veces cuenta 1), la actividad del dueño nunca cuenta, los
usuarios suspendidos/baneados no cuentan, escala logarítmica con tope, un precio < 20% de la mediana (carnada) no se
premia, dueños con advertencias de evasión −20%, y cada exposición en el canal resta `30·e^(−h/decay)` (tope 60)
para que el mismo artículo no sature. `GET /admin/items/{id}/score` muestra el desglose. La IA no toca estos números.

**Diversidad (sección 28)** y **control de exposición (sección 29)** se aplican a la lista sugerida: penalidades por
repetir categoría, zona, rango de precio, permuta y antigüedad; máximo un artículo por dueño; máximo
`ceil(limit/3)` por categoría; nunca dos de la misma categoría seguidos (salvo que no quede otra cosa); cupo diario
(posts en las últimas 24h + en cola), máximo de exposiciones por artículo, horas mínimas entre repeticiones, y
minutos mínimos entre posts (informativo: `next_post_allowed_at`). Los destacados reciben +15 de prioridad (hasta
`channel_featured_max_per_batch` por lista) pero **siguen sujetos al puntaje mínimo**.

**`channel_status` es independiente del estado de la publicación** (sección 33): `NOT_POSTED`, `QUEUED`, `VISIBLE`,
`REMOVAL_PENDING`, `REMOVED`. Bajar un post nunca cambia la publicación.

| Endpoint (`/admin`) | Rol | Uso |
|---|---|---|
| `GET /channel/suggestions?limit=` | VIEWER | lista ordenada con puntaje, desglose, cupo y motivos de exclusión |
| `POST /channel/queue` `{item_ids}` | MODERATOR | planificar posts (`PENDING`), respeta cupo y un post vivo por artículo |
| `POST /channel/posts/{id}/published` `{external_ref, position}` | MODERATOR | "lo publiqué" → `PUBLISHED` + exposición |
| `POST /channel/posts/{id}/failed` / `retry` | MODERATOR | no se pudo publicar / reintentar |
| `POST /channel/posts` `{item_id, external_ref}` | MODERATOR | registrar directo un post ya publicado |
| `POST /channel/posts/{id}/remove` `{reason}` | MODERATOR | pedir la baja de un post (rotación, etc.) |
| `POST /channel/posts/{id}/removed` | MODERATOR | "lo borré del canal" |
| `GET /channel/removals` | VIEWER | **qué hay que borrar ya** (con horas de espera y `overdue`) |
| `POST /items/{id}/feature` `{featured}` | MODERATOR | destacar / quitar destacado |
| `GET /channel/overview`, `GET /channel/posts` | VIEWER | resumen, cupo, jobs, último informe de reconciliación |
| `POST /channel/reconcile`, `POST /channel/refresh-scores` | MODERATOR | correr a mano |

La realidad manda: si el admin confirma un post de un artículo que se cerró mientras estaba en cola, se registra y
queda en `REMOVAL_PENDING` de inmediato.

**Reconciliación** (`channel.reconcile`, cada 10 min). Corrige solo lo seguro (y lo audita): artículos cerrados o
dueños bloqueados con post visible → `REMOVAL_PENDING`; posts en cola > 24h → `FAILED`; posts publicados sin
exposición → se crea; `channel_status` desalineado → se corrige. Informa para el admin: bajas atrasadas
(`channel_removal_overdue_hours`), `external_ref` duplicados, sobre-exposición, posts fallidos, posts visibles de
artículos pausados o con reportes abiertos, posts para refrescar (`channel_refresh_hours`) y destacados listos.

Claves de `system_config` del canal (editables con `PUT /admin/config`): `channel_enabled`, `channel_daily_limit`,
`channel_min_score`, `channel_max_exposure_per_item`, `channel_min_hours_between_same_item`,
`channel_category_diversity`, `channel_zone_diversity`, `channel_price_diversity`,
`channel_featured_max_per_batch`, `channel_min_minutes_between_posts`, `channel_exposure_decay_hours`,
`channel_refresh_hours`, `channel_removal_overdue_hours`.

## Métricas (sección 48)

`GET /admin/metrics?days=30` (VIEWER, sin datos personales) devuelve lo que necesita el dashboard de la app Android:
usuarios (total, nuevos, activos, suspendidos, baneados), publicaciones por estado y en el período, búsquedas
(sesiones, usuarios distintos, sin resultados), ofertas (por estado, tasas de aceptación / rechazo / vencimiento),
pagos (por estado, aprobados, reembolsos pendientes, discrepancias, **recaudación** solo de pagos verificados),
contactos liberados, conversión (búsqueda→oferta→aceptación→pago), canal (posts, exposiciones, bajas pendientes,
destacados), errores (outbox, cola de entrada, jobs fallidos/abandonados, eventos de seguridad), bans,
rendimiento (p50/p95 de entrega del outbox, procesamiento de entrada y respuesta del bot; estado de jobs; workers
vivos) y una serie diaria en hora de Argentina (`daily`).
## Consola Android de administración (fase 4)

`android/admin-console/` — app nativa Kotlin + Jetpack Compose, MVVM + Clean Architecture (dominio sin tipos
Android, repositorios, ViewModels con `StateFlow`), Retrofit + OkHttp + kotlinx.serialization, DI manual. Detalle
completo en [`android/admin-console/README.md`](android/admin-console/README.md).

* **Pantallas (sección 8):** Login, Dashboard (métricas), Usuarios (búsqueda/perfil/suspender/banear/rehabilitar),
  Publicaciones (detalle, edición con motivo, eliminar, destacar, puntaje), Ofertas, Operaciones, Pagos,
  Conversaciones (solo MODERATOR+, cada apertura auditada), Seguridad (baneos pendientes, advertencias, reportes,
  eventos, auditoría), Canal (sugerencias → cola → publicado/fallido, registro manual, bajas, reconciliar,
  recalcular, destacados, estado de workers), Configuración (precio, vencimientos, límites, canal, modo del sistema
  con motivo), Sistema (estado de servicios, workers y jobs).
* **Sin credenciales de terceros:** la app solo conoce la URL del backend y el JWT del admin. El test
  `backend/tests/unit/test_android_contract.py` busca patrones de credenciales en el código de la app y además
  verifica que **cada** ruta, parámetro y cuerpo que usa la app existe en la API real (49 operaciones).
* **Seguridad:** solo HTTPS en release (`network_security_config`, validación de URL, OkHttp `MODERN_TLS`; HTTP solo
  en debug hacia `10.0.2.2`/`localhost`), tokens cifrados con AES-256-GCM (clave no exportable del Keystore) en
  DataStore, refresh con rotación y una sola renovación ante 401 concurrentes, bearer solo al host de la sesión, sin
  logging de red, `allowBackup=false`, `FLAG_SECURE`. La autoridad de permisos es el backend (RBAC).
* **Endpoints agregados para la consola** (`backend/app/api/routes/admin/console.py`): `GET/PATCH
  /admin/items/{id}`, `GET /admin/transactions/{id}`, `GET /admin/conversations`, `GET
  /admin/conversations/{user_id}/context`, `GET /admin/security/warnings`, `GET /admin/audit`; el perfil de usuario
  incluye su actividad.

### Compilar, probar y APK

```bash
cd android/admin-console          # requiere JDK 17 y Android SDK (platforms;android-34, build-tools;34.0.0)
echo "sdk.dir=$HOME/Android/Sdk" > local.properties    # no versionado
./gradlew assembleDebug           # app/build/outputs/apk/debug/app-debug.apk
./gradlew testDebugUnitTest       # 45 tests JVM + Robolectric
./gradlew lintDebug
./gradlew connectedDebugAndroidTest   # en TU dispositivo/emulador (no se corrió aquí)
./gradlew assembleRelease -Ppermutados.apiBaseUrl=https://api.tu-dominio/
```

APK de release **firmado**: crear un keystore fuera del repo y `android/admin-console/keystore.properties`
(`storeFile`, `storePassword`, `keyAlias`, `keyPassword`; ignorado por git) — pasos en el README de la app. Sin ese
archivo se genera `app-release-unsigned.apk`. Instalar en el teléfono del admin con `adb install` o copiando el APK.

**Qué se ejecutó (2026-10-08):** `assembleDebug` (APK debug 11,5 MB), `assembleDebugAndroidTest`,
`assembleRelease` sin firmar (1,7 MB, R8), `testDebugUnitTest` **45/45**, `lintDebug` 0 errores / 39 avisos.
**No se ejecutó:** los 3 tests instrumentados (`KeystoreTokenCipherTest`, necesitan el Keystore real de un
dispositivo) ni la instalación en un dispositivo: el único emulador de la máquina de construcción pertenece a otro
proyecto y no se tocó, y no había otro dispositivo. La app nunca se abrió en un teléfono real.

## Backups y recuperación ante desastres (sección 45)

`scripts/backup_postgres.py` (sin dependencias; lo llama `scripts/backup_postgres.sh` desde cron):

* `pg_dump -Fc` desde un **snapshot exportado** (`pg_export_snapshot` + `--snapshot`): el dump y el conteo de filas
  del manifiesto ven exactamente los mismos datos aunque la app siga escribiendo.
* Se escribe a `.partial`, se verifica con `pg_restore --list`, se calcula `sha256`, se guarda un manifiesto
  `.json` (revisión de Alembic, filas por tabla, tamaño) y recién entonces se renombra. Permisos `0600` / `0700`.
* Rotación: borra backups más viejos que `BACKUP_RETENTION_DAYS` pero **siempre conserva los `BACKUP_KEEP_MIN` más
  nuevos**; limpia `.partial` de corridas caídas. Copia fuera del servidor opcional con `BACKUP_COPY_CMD`
  (p. ej. `rclone copy {file} remoto:permutados`).
* La contraseña va por `PGPASSWORD` (nunca en la línea de comandos ni en la salida). Salida JSON, exit ≠ 0 si falla.
* Modo `native` (pg_dump del host + `DATABASE_URL`) o `docker` (`docker compose exec postgres pg_dump`).

```bash
# cron diario (04:15)
15 4 * * * /opt/permutados/scripts/backup_postgres.sh >> /var/log/permutados-backup.log 2>&1
# a mano
DATABASE_URL=postgresql://permutados:***@localhost:5432/permutados python3 scripts/backup_postgres.py --dir /backups
```

**Restaurar** (`scripts/restore_postgres.py`): verifica checksum y archivo antes de tocar nada, se niega a pisar una
base con tablas salvo `--force`, restaura en **una sola transacción** (`--single-transaction --exit-on-error`) y
compara revisión y filas con el manifiesto. `--verify-only` solo verifica.

Procedimiento ante desastre:

1. Detener backend y worker (`docker compose stop backend worker`).
2. Elegir el backup: `python3 scripts/restore_postgres.py /backups/permutados_<fecha>.dump --verify-only`.
3. Restaurar en una base nueva y revisar:
   `python3 scripts/restore_postgres.py <archivo> --database-url postgresql://…/permutados_restore --create`
   (o sobre la base de producción con `--force`).
4. Poner la **misma `ENCRYPTION_KEY`** que tenía el sistema cuando se hizo el backup (los teléfonos y nombres
   están cifrados; **la clave no está en el backup** y debe guardarse aparte, p. ej. en un gestor de secretos o
   impresa en lugar seguro). Sin la clave, los datos personales son irrecuperables.
5. `alembic upgrade head` si el backup es de una versión anterior.
6. Arrancar el worker: al iniciar recupera jobs y mensajes en vuelo, vence lo atrasado y consulta a Mercado Pago
   los pagos pendientes (los aprobados durante la caída se honran). Después arrancar el backend.
7. Revisar `GET /admin/workers`, `GET /admin/channel/removals` y el informe de reconciliación.

Probado de verdad contra el PostgreSQL local (`tests/integration/test_backup_restore.py`): backup → restauración en
una base nueva → filas y revisión iguales, teléfono descifrable con la clave, trigger append-only intacto; backup
consistente con escrituras concurrentes; rechazo de archivos corruptos/truncados; rotación y copia externa; wrapper
de cron; y migración `0003` ida y vuelta sobre la base restaurada. El modo `docker` está escrito pero **no se pudo
probar** en este entorno (no hay Docker).
## Producción

Checklist antes de abrir al público:

1. Servidor con PostgreSQL 17, Redis y Python 3.13 (nativo con systemd) **o** Docker (`docker compose up -d`).
2. Dominio con **TLS** (nginx/Caddy) apuntando al backend; solo `/webhooks`, `/admin`, `/health` expuestos;
   `FORWARDED_ALLOW_IPS` = IP del proxy. Firewall: PostgreSQL, Redis y Dolphin solo en red interna.
3. `.env` con `ENVIRONMENT=production`, secretos reales (permisos `600`), `CORS_ALLOWED_ORIGINS` explícito
   (la app Android no lo necesita), `MERCADOPAGO_ENVIRONMENT=production`.
4. `ENCRYPTION_KEY` generada y **copiada fuera del servidor** (gestor de secretos / papel en lugar seguro).
5. `alembic upgrade head`, crear el SUPERADMIN con contraseña fuerte; crear usuarios MODERATOR/VIEWER para el día a día.
6. Webhooks dados de alta en Meta y Mercado Pago; plantilla `permutados_novedad` **aprobada**.
7. Worker corriendo (una sola réplica alcanza; varias son seguras por advisory locks). Verificar en la consola →
   **Sistema** (o `GET /admin/system/health`, `GET /admin/workers`).
8. Backups: cron de `scripts/backup_postgres.sh` + copia externa (`BACKUP_COPY_CMD`) y **probar una restauración**.
9. Prueba completa con números reales en sandbox: publicar, ofertar, aceptar, pagar con usuario de prueba de MP,
   verificar liberación y palabra clave.
10. Consola Android: APK de release firmado con la URL HTTPS del backend.

Ejemplo systemd (nativo):

```ini
# /etc/systemd/system/permutados-api.service   (y permutados-worker.service con ExecStart=... -m app.workers.scheduler)
[Service]
User=permutados
WorkingDirectory=/opt/permutados/backend
EnvironmentFile=/opt/permutados/.env
ExecStart=/opt/permutados/.venv/bin/uvicorn --factory app.main:create_app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
```

**Modos de operación** (consola → Configuración, o `PUT /admin/system/mode`): `PAUSED` para mantenimiento (se sigue
publicando y buscando, se congelan plazos), `EMERGENCY` ante un incidente (todo lo que cambia estado bloqueado,
mensajes sensibles retenidos). Al volver a `ONLINE` los plazos se extienden y se avisa a los afectados.

## Tests

Corren contra **PostgreSQL y Redis reales** (la base de tests se borra y se recrea; la migración se prueba con
upgrade → downgrade → upgrade al inicio, y la `0002` y la `0003` con datos reales ida y vuelta).

```bash
cd backend && ../.venv/bin/pytest -q
# o: scripts/run_tests.sh   (ruff + pytest)
# variables: TEST_DATABASE_URL (default …/permutados_test), TEST_REDIS_URL (default redis://localhost:6379/15)
```

| Carpeta | Qué cubre |
|---|---|
| `tests/unit` | imágenes, intenciones, parser de búsqueda, anti-evasión, cifrado, firmas, tokens, plantillas, clientes WhatsApp/MP (requests reales verificados con `httpx.MockTransport`), ranking del canal, `.env.example`, **contrato Android ↔ backend** |
| `tests/integration` | publicaciones, búsqueda/paginación/no repetición, ofertas (simultáneas, segunda oferta, 24h), pagos y validaciones, cola de entrada, outbox y recuperación, máquina de estados, modos, workers (proceso real con SIGKILL/SIGTERM), canal, eventos, backup/restore real, API admin de la consola |
| `tests/security` | permisos administrativos (RBAC, lockout, refresh con reutilización), webhooks (firmas, replay), prompt injection, cifrado de datos personales |
| `tests/e2e` | flujo completo y las ramas de la sección 50 (ver abajo) |
| `android/admin-console/app/src/test` | 45 tests: URL segura, roles, redacción, errores, red contra MockWebServer (login, refresh, 401 concurrentes, origen), DataStore cifrado, ViewModels, UI de login (Robolectric) |
| `android/admin-console/app/src/androidTest` | `KeystoreTokenCipherTest` (requiere dispositivo; no ejecutado aquí) |

Última corrida: **357 passed, 0 failed** (backend, ~75 s) y **45/45** (Android unit).

## E2E obligatorio (sección 50)

Todos atraviesan el sistema como en producción: webhooks firmados de WhatsApp y Mercado Pago → cola de entrada →
worker → máquina de estados → outbox → jobs reales del scheduler → API admin HTTP. Solo se simulan Dolphin (guion
por turno), la red de Meta y la de Mercado Pago.

| Caso | Test |
|---|---|
| Flujo completo: foto → publicación → búsqueda (5 resultados) → selección → oferta → A acepta → solicitud de pago → MP confirma → backend verifica → contacto + palabra clave a ambos → `COMPLETED` → post del canal `REMOVAL_PENDING` → baja confirmada | `test_e2e_flow.py`, `test_whatsapp_e2e.py` |
| Rechazo | `test_e2e_branches.py::test_owner_rejects_the_offer` |
| 24h sin respuesta (aviso a ambos) | `::test_offer_without_answer_expires_after_24h_and_both_are_told` |
| No pago en 24h | `::test_accepted_but_not_paid_within_24h` |
| Pago falso ("ya pagué", firma falsa, id desconocido, monto/moneda/estado incorrectos) | `::test_fake_payments_never_release_contacts` |
| Webhook duplicado (WhatsApp y MP, replay y nuevo request id) | `::test_duplicate_webhooks_release_exactly_once` |
| Segunda oferta sobre artículo pausado | `::test_second_offer_is_refused_while_the_item_is_paused` |
| Cancelación (ofertante; dueño se arrepiente antes del pago; no puede después) | `::test_cancellations` |
| 1er intento de evasión → advertencia; 2do → suspensión + revisión admin → ban desde la API | `::test_first_evasion_warns_second_suspends_for_admin_review` |
| Error de WhatsApp (reintento sin pérdida) | `::test_whatsapp_send_error_is_retried_not_lost` |
| Error de Mercado Pago (link, webhook durante caída, vencimiento no cancela, recuperación) | `::test_mercadopago_errors_are_safe_and_recoverable` |
| Dolphin caído y de vuelta | `::test_dolphin_down_then_back` |
| Reinicio del servidor y recuperación (mensaje y pago durante la caída) | `::test_server_restart_recovery` |
| Reconciliación del canal (eventos perdidos) | `::test_channel_reconciliation_catches_missed_events` |

Nota: por decisión del dueño, el **2do intento de evasión suspende la cuenta y la deja pendiente de revisión**; el
ban lo confirma un admin (la sección 50 dice "segundo intento → ban").

## Definition of Done (sección 55)

| Ítem | Estado | Evidencia |
|---|---|---|
| backend funciona | ✅ | 357 tests; arranque real de API (`/health`, `/health/ready` OK) y worker (recuperación, 12 jobs, parada con SIGTERM) |
| Android compila | ✅ | `assembleDebug`, `assembleRelease` (sin firmar), `assembleDebugAndroidTest`; 45/45 unit tests |
| Docker funciona | ⚠️ no verificado | `docker-compose.yml` + Dockerfile escritos y revisados; Docker no está instalado en la máquina de construcción |
| PostgreSQL / Redis | ✅ | todos los tests corren contra PostgreSQL 17 y Redis reales |
| Alembic | ✅ | upgrade/downgrade/upgrade en cada corrida, migraciones con datos, `alembic check` sin drift |
| Dolphin integrado | ✅ (mock) | cliente OpenAI-compatible + allowlist de herramientas; probado con `MockDolphinClient`; sin servidor real |
| WhatsApp preparado | ✅ (sin credenciales) | cliente Cloud API, webhooks firmados, ventana 24h, plantilla; nunca se envió un mensaje real |
| Mercado Pago preparado | ✅ (sin credenciales) | Checkout Pro, webhook firmado + consulta, validaciones; nunca se cobró un pago real/sandbox |
| Channel Worker / Reconciliation | ✅ | `test_channel_*`, `test_e2e_branches.py::test_channel_reconciliation_catches_missed_events` |
| scheduler | ✅ | proceso real en tests (SIGKILL/reinicio) y smoke run |
| seguridad implementada | ✅ | RBAC, argon2id, JWT con rotación, lockout, firmas, cifrado de datos personales, anti prompt injection, redacción de logs, app sin credenciales |
| backups configurados | ✅ (modo nativo) | backup/restore real probado; modo docker no probado; cron a configurar en el servidor |
| tests / E2E pasan | ✅ | 357/357 backend, 45/45 Android unit; tests instrumentados no ejecutados |
| README / `.env.example` completos | ✅ | este archivo; `tests/unit/test_env_example.py` |
| sin secretos / Git limpio | ✅ | `.env`, keystores y `local.properties` ignorados; escaneo de secretos antes de cada push |

## Qué es real y qué es simulado

* **Real y probado:** PostgreSQL, Redis, migraciones, toda la lógica de dominio, la API HTTP, la verificación de
  firmas de los webhooks (payloads con la forma real de Meta/MP), la cola de entrada, el outbox, la máquina de
  estados, el cifrado, el proceso worker (subproceso real con caída SIGKILL y reinicio), advisory locks,
  `pg_dump`/`pg_restore`, la compilación de la app Android y sus tests JVM.
* **Implementado según la documentación oficial pero sin credenciales reales:** cliente de WhatsApp Cloud API y de
  Mercado Pago. Sus requests (URL, headers, cuerpo, reintentos, errores) están verificados con `httpx.MockTransport`
  y en los E2E se reemplazan por `RecordingWhatsAppClient` y `FakeGateway`. **Nunca se envió un WhatsApp real ni
  se cobró un pago real/sandbox.**
* **Simulado:** Dolphin (`MockDolphinClient`); el cliente OpenAI-compatible existe pero no hay servidor.
* **No ejecutado:** Docker / `docker compose`, backup en modo docker, tests instrumentados Android, app en un
  dispositivo, APK firmado.

## Troubleshooting

| Síntoma | Causa probable / solución |
|---|---|
| El backend no arranca: `ENCRYPTION_KEY` | falta o no es base64 de 32 bytes; generarla (ver `.env.example`) |
| El backend no arranca en producción | configuración insegura: falta un secreto, `CORS_ALLOWED_ORIGINS=*`, o pasarela simulada; el mensaje dice cuál |
| Meta no valida el webhook | `WHATSAPP_VERIFY_TOKEN` distinto del cargado en Meta, o la URL no es HTTPS pública |
| Webhooks de WhatsApp → 401 | `WHATSAPP_APP_SECRET` incorrecto o el proxy modifica el cuerpo |
| Webhooks de MP → 401 | `MERCADOPAGO_WEBHOOK_SECRET` incorrecto o reloj del servidor desfasado (> `MERCADOPAGO_SIGNATURE_TOLERANCE_SECONDS`); usar NTP |
| Mensajes en `AWAITING_WINDOW` | el usuario no escribió en 24h y la plantilla `permutados_novedad` no está aprobada o tiene otro nombre/idioma |
| Error 131047 en notificaciones | fuera de la ventana de 24h: comportamiento esperado, se usa la plantilla |
| Un pago aprobado no liberó | ver consola → Pagos: `NEEDS_REFUND` (fuera de término/duplicado/monto) o `PAYMENT_MISMATCH` en Seguridad; si el webhook se perdió, `reconcile_pending_payments` lo encuentra en ≤ 5 min |
| El bot responde "servicio no disponible" | Dolphin sin configurar o caído (`DOLPHIN_BASE_URL`); consola → Sistema |
| Nada vence / no salen mensajes | el worker no corre: consola → Sistema o `GET /admin/workers`; arrancar `python -m app.workers.scheduler` |
| Plazos que "no corren" | el sistema está en `PAUSED`/`EMERGENCY` (los plazos se congelan a propósito) |
| Login admin bloqueado | 5 intentos fallidos → bloqueo de 15 min (`ADMIN_MAX_FAILED_LOGINS`, `ADMIN_LOCKOUT_MINUTES`) |
| La app Android rechaza la URL | release exige `https://`; HTTP solo en debug hacia `10.0.2.2`/`localhost` (emulador → backend local) |
| La app vuelve al login sola | el refresh token venció (7 días) o fue revocado; si se reinstaló la app, la clave del Keystore cambió |
| `alembic check` reporta cambios | el modelo cambió sin migración: generar una con `--autogenerate` y revisarla |
| Tests fallan al conectar | PostgreSQL/Redis no corren o `TEST_DATABASE_URL`/`TEST_REDIS_URL` incorrectos (la base de tests se borra) |

## Pendiente del dueño antes de producción

1. **Meta / WhatsApp:** app Business, número, token permanente de usuario de sistema, app secret, verify token,
   webhook dado de alta, y **aprobar la plantilla UTILITY `permutados_novedad`** (es_AR, una variable `{{1}}`).
2. **Mercado Pago:** credenciales de prueba (sandbox) y luego de producción, secreto del webhook, URL del webhook;
   hacer una prueba con usuarios de prueba.
3. **Servidor Dolphin:** levantarlo (vLLM u otro OpenAI-compatible con tool calling) y configurar
   `DOLPHIN_BASE_URL`; ajustar prompts con conversaciones reales.
4. **`ENCRYPTION_KEY`:** generarla, ponerla en `.env` y **guardar una copia segura fuera del servidor**. Sin ella
   los teléfonos y nombres son irrecuperables (no está en los backups).
5. `ADMIN_AUTH_SECRET`, contraseña de PostgreSQL, dominio con TLS y reverse proxy.
6. **Docker** (opcional): instalarlo si se quieren contenedores, y probar `docker compose up` por primera vez.
7. **Android:** keystore de firma (fuera del repo) para el APK de release, `permutados.apiBaseUrl` con la URL real,
   y correr `./gradlew connectedDebugAndroidTest` en un dispositivo propio.
8. Cron de backups + copia externa, y una restauración de prueba.

## Limitaciones conocidas

* Ninguna integración externa fue verificada con credenciales reales (WhatsApp, Mercado Pago, Dolphin).
* El **Canal de WhatsApp es manual**: la API oficial no permite publicar en Canales; el backend solo sugiere,
  registra y vigila (no se usa automatización no oficial).
* Docker, el backup en modo docker y el despliegue de producción no se ejecutaron en la máquina de construcción.
* Android: tests instrumentados no ejecutados, app nunca abierta en un dispositivo real, APK de release sin firmar,
  sin *certificate pinning* (se confía en las CA del sistema), dependencias fijadas en versiones estables de 2024
  (lint avisa de versiones más nuevas), las métricas del dashboard se muestran de forma genérica (árbol JSON
  legible), sin modo offline.
* El panel web (`admin-web/opcional`) no está implementado.
* Las métricas de rendimiento salen de la base (p50/p95 por job/mensaje); no hay Prometheus/Grafana.

## Plan de fases

1. **Fase 1 ✅:** backend, modelo, servicios, capa Dolphin, Admin API, webhooks, tests.
2. **Fase 2 ✅:** motor de conversación, cliente WhatsApp Cloud API con ventana de 24h y plantilla, outbox,
   Mercado Pago Checkout Pro, cifrado de datos personales.
3. **Fase 3 ✅:** workers recuperables, outbox sin duplicados, bus de eventos, canal manual completo, modos en
   workers, backups/restauración, métricas.
4. **Fase 4 ✅:** consola Android de administración, endpoints admin faltantes, E2E de la sección 50, cierre.
5. **Siguiente (dueño):** credenciales reales, servidor Dolphin, despliegue, APK firmado.

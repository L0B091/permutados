# PERMUTADOS

Marketplace de compra/venta/permuta que funciona **dentro de WhatsApp**. Un bot 1:1 (WhatsApp Cloud API)
conversa con el usuario mediante un LLM local (**Dolphin-Mistral-24B-Venice-Edition**, servido con una API
compatible con OpenAI). El backend es la única fuente de verdad: Dolphin solo puede *pedir* acciones a través
de una lista cerrada de herramientas; los permisos, estados, pagos y la liberación de contactos los decide el
backend.

> Repositorio privado. Todos los derechos reservados (ver `LICENSE`).

## Estado: Fase 2 completa (backend + mensajería real)

| Área | Estado |
|---|---|
| Modelo de datos (23 tablas, enums, constraints, triggers, índices parciales, FTS español) | ✅ Fase 1 |
| Migraciones Alembic `0001` + `0002` (upgrade / downgrade con datos / `alembic check` sin drift) | ✅ |
| Publicaciones, imágenes, búsqueda, ofertas, pagos, liberación, anti-evasión, Admin API, modos, canal manual | ✅ Fase 1 |
| **Motor de conversación**: webhook WhatsApp → cola durable → worker → intención → máquina de estados → Dolphin → herramientas | ✅ Fase 2 |
| **Cliente WhatsApp Cloud API**: texto, imagen, plantilla, marcar leído, descarga de media, reintentos, idempotencia | ✅ Fase 2 |
| **Ventana de 24h**: sesión gratis dentro de la ventana; fuera, una plantilla aprobada y el resto espera | ✅ Fase 2 |
| **Outbox** despachado por el worker (at-least-once, backoff, `SKIP LOCKED`, estados de entrega de Meta) | ✅ Fase 2 |
| **Mercado Pago Checkout Pro** real (preferencia ARS 3000, webhook `payment` + `merchant_order`, validaciones) | ✅ Fase 2 |
| **Cifrado a nivel campo** de teléfonos, nombres y mensajes sensibles (AES-256-GCM + índice ciego) | ✅ Fase 2 |
| Pruebas con credenciales sandbox reales de Meta / Mercado Pago | ⏳ requiere credenciales del dueño |
| Instalación del servidor Dolphin | ⏳ cuando exista el servidor físico |
| Consola Android de administración | ⏳ Fase 3 |

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
                                │
                                ▼
                    notifications (outbox) ─► Worker: dispatch_outbox ─► WhatsAppClient (texto / plantilla)
Mercado Pago ──► /webhooks/mercadopago (firma) ─► consulta API ─► valida ─► liberación ─► outbox (cifrado)
PostgreSQL = fuente de verdad · Redis = rate limiting · Admin (Android, fase 3) ──► /admin/* (JWT + RBAC)
```

```
backend/            FastAPI + SQLAlchemy 2 + Alembic
  app/core/         config, db, seguridad (argon2id, JWT), rate limit, logging con redacción de secretos
  app/models/       modelo de datos
  app/services/     lógica de dominio (cada módulo es independiente y testeable)
  app/api/          rutas HTTP (health, webhooks, admin)
  app/workers/      jobs periódicos
  migrations/       Alembic
  tests/            unit / integration / security / e2e
dolphin/            prompts, políticas y definición de herramientas (montado como volumen)
docker/             Dockerfile del backend, nginx de referencia
scripts/            create_admin, backups, tests, export de herramientas
android/admin-console/  (fase 3)
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
  de atractivo y sugiere artículos (`GET /admin/channel/suggestions`); el admin publica a mano y registra el post
  (`POST /admin/channel/posts`). Cuando un artículo se cierra (completado, vencido o borrado) su post queda en
  `REMOVAL_PENDING` y el admin lo borra a mano y lo confirma (`POST /admin/channel/posts/{id}/removed`).
* **Modos:** `PAUSED` bloquea nuevas ofertas, aceptaciones y links de pago, y difiere las liberaciones (publicar y
  buscar siguen funcionando); `EMERGENCY` además bloquea publicar y editar. Los pagos que llegan por webhook se
  registran siempre y la liberación se completa al volver a `ONLINE`.

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

## Mensajería WhatsApp (fase 2)

* **Entrada:** `POST /webhooks/whatsapp` verifica `X-Hub-Signature-256`, deduplica por `message_id`, cifra la
  identidad y el texto en un sobre (`webhook_events`) y responde enseguida. El worker
  (`process_inbound_queue`, cada `INBOUND_POLL_SECONDS`) procesa la cola con un advisory lock (un solo consumidor →
  orden por usuario), descarga la imagen (con reintentos, límite de tamaño y verificación sha256), corre la
  conversación y **borra el sobre** al terminar. Errores transitorios → reintento con backoff; después de
  `INBOUND_MAX_ATTEMPTS` → `FAILED`. Tipos no soportados (audio, video, sticker…) reciben una respuesta fija;
  ubicaciones, un aviso (solo zona general); **tarjetas de contacto cuentan como intento de evasión**.
* **Salida (outbox):** todo mensaje saliente es una fila de `notifications` creada en la misma transacción que el
  cambio de dominio. `dispatch_outbox` (cada `OUTBOX_POLL_SECONDS`) toma filas con `FOR UPDATE SKIP LOCKED`, una
  transacción por mensaje, backoff exponencial, `OUTBOX_MAX_ATTEMPTS`. Entrega *at-least-once*: el cliente no
  reintenta envíos ante 5xx/timeout (Meta pudo haberlo aceptado); reintenta el outbox.
* **Ventana de 24h:** cada mensaje entrante guarda `users.last_inbound_at`. Dentro de la ventana (con un margen de
  `WHATSAPP_WINDOW_SAFETY_MINUTES`) se manda texto libre (gratis). Fuera de la ventana se manda **una sola**
  plantilla aprobada por período de silencio (máx. 1 cada 24h) con un resumen sin datos personales, y los mensajes
  completos quedan en `AWAITING_WINDOW` hasta que la persona responda; los que tienen vencimiento (avisos de
  oferta/pago) pasan a `EXPIRED` si caducan antes. El error 131047 de Meta (fuera de ventana) se maneja tanto en la
  respuesta como en los webhooks de estado (`sent/delivered/read/failed` se guardan en la notificación).
* **Plantilla requerida en Meta:** crear y aprobar en WhatsApp Manager una plantilla **UTILITY** llamada
  `permutados_novedad` (o la de `WHATSAPP_FALLBACK_TEMPLATE`), idioma **es_AR**, con **una** variable `{{1}}`
  en el cuerpo. Ej.: *"Tenés una novedad en Permutados: {{1}}. Respondé este mensaje para verla."*
* `biz_opaque_callback_data` lleva la clave de idempotencia de la notificación.

## Pagos con Mercado Pago (fase 2)

* `create_payment` crea una preferencia **Checkout Pro** real: 1 ítem (categoría `services`), ARS
  `CONTACT_RELEASE_PRICE`, `binary_mode`, sin efectivo/ticket, 1 cuota, `external_reference` único,
  `notification_url`, vencimiento = fin de la ventana de pago, `statement_descriptor`, `X-Idempotency-Key`
  determinística. Sandbox usa `sandbox_init_point`.
* El webhook (firma `x-signature` v1 + tolerancia de tiempo) **nunca confía en el cuerpo**: consulta
  `GET /v1/payments/{id}` (o `GET /merchant_orders/{id}` → pagos) y valida estado, monto, moneda,
  `external_reference`, operación, `live_mode` (en producción se rechazan pagos sandbox) y, si se configura,
  `MERCADOPAGO_COLLECTOR_ID`. Discrepancias → `PAYMENT_MISMATCH` (evento de seguridad + auditoría), sin liberar.
  Procesamiento idempotente (mismo pago dos veces = una liberación).

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

## Desarrollo local (sin Docker)

Requisitos: Python 3.13, PostgreSQL 17, Redis 7+.

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cp .env.example .env    # completar; ENCRYPTION_KEY es obligatoria (ver comando en .env.example)
createdb permutados && createdb permutados_test
cd backend
../.venv/bin/alembic upgrade head
../.venv/bin/python ../scripts/create_admin.py admin SUPERADMIN
../.venv/bin/uvicorn --factory app.main:create_app --reload     # API
../.venv/bin/python -m app.workers.scheduler                    # worker
```

## Producción (Docker)

```bash
cp .env.example .env    # ENVIRONMENT=production, secretos reales, ADMIN_AUTH_SECRET >= 32 chars
docker compose up -d --build
docker compose exec backend python /srv/scripts/create_admin.py admin SUPERADMIN   # o desde backend/
```

El backend se niega a arrancar en producción con configuración insegura (secretos faltantes, CORS `*`) o con
la pasarela de pagos simulada. Exponer solo `/webhooks`, `/admin` y `/health` detrás de TLS
(ver `docker/nginx.conf`). Backups: `scripts/backup_postgres.sh` (cron diario, retención configurable).

### Dolphin (cuando esté el servidor)

Servir el modelo con cualquier servidor OpenAI-compatible con *tool calling* habilitado, por ejemplo vLLM:

```bash
vllm serve dphn/Dolphin-Mistral-24B-Venice-Edition --enable-auto-tool-choice --tool-call-parser mistral
```

y configurar `DOLPHIN_BASE_URL=http://<servidor>:8000/v1`. Prompts y políticas se editan en `dolphin/` sin
tocar código.

## Tests

Corren contra **PostgreSQL y Redis reales** (la base de tests se borra y se recrea; la migración se prueba con
upgrade → downgrade → upgrade al inicio, y la `0002` con datos reales ida y vuelta).

Qué es real y qué es simulado en los tests:

* **Real:** PostgreSQL, Redis, migraciones, toda la lógica de dominio, la API HTTP (TestClient), la verificación de
  firmas de los webhooks de WhatsApp y Mercado Pago (payloads con la forma real de Meta/MP, firmados), la cola de
  entrada, el outbox, la máquina de estados, el cifrado, la construcción de requests HTTP de los clientes de
  WhatsApp y Mercado Pago (verificada con `httpx.MockTransport`: URLs, headers, cuerpos, reintentos, errores).
* **Simulado:** el modelo Dolphin (`MockDolphinClient` con guiones por turno), la red hacia Meta
  (`RecordingWhatsAppClient`) y hacia Mercado Pago (`FakeGateway`). No se usaron credenciales reales.
* `tests/e2e/test_whatsapp_e2e.py` recorre el flujo completo solo con webhooks: foto → identificación →
  datos → vista previa → publicar → búsqueda (5) → "más" → selección → oferta (avisos de 24h a ambos) →
  ACEPTAR → PAGAR (link enviado por el backend) → webhook MP firmado → ambos reciben número + palabra clave, y
  verifica que la base no contiene ningún teléfono ni nombre en texto plano.

```bash
cd backend && ../.venv/bin/pytest -q
# o: scripts/run_tests.sh   (ruff + pytest)
# variables: TEST_DATABASE_URL, TEST_REDIS_URL
```

## Plan de fases

1. **Fase 1 ✅:** backend, modelo, servicios, capa Dolphin, Admin API, webhooks, tests.
2. **Fase 2 ✅:** motor de conversación (cola + worker + intención + máquina de estados), cliente WhatsApp Cloud
   API con ventana de 24h y plantilla, outbox, Mercado Pago Checkout Pro, cifrado de datos personales.
   Pendiente del dueño: credenciales sandbox de Meta/MP y aprobación de la plantilla `permutados_novedad`.
3. **Fase 3:** consola Android de administración.
4. **Fase 4:** servidor Dolphin físico, ajuste de prompts con conversaciones reales.

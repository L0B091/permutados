# PERMUTADOS

Marketplace de compra/venta/permuta que funciona **dentro de WhatsApp**. Un bot 1:1 (WhatsApp Cloud API)
conversa con el usuario mediante un LLM local (**Dolphin-Mistral-24B-Venice-Edition**, servido con una API
compatible con OpenAI). El backend es la única fuente de verdad: Dolphin solo puede *pedir* acciones a través
de una lista cerrada de herramientas; los permisos, estados, pagos y la liberación de contactos los decide el
backend.

> Repositorio privado. Todos los derechos reservados (ver `LICENSE`).

## Estado: Fase 3 completa (automatización, canal manual, resiliencia)

| Área | Estado |
|---|---|
| Modelo de datos (23 tablas, enums, constraints, triggers, índices parciales, FTS español) | ✅ Fase 1 |
| Migraciones Alembic `0001` + `0002` + `0003` (upgrade / downgrade con datos / `alembic check` sin drift) | ✅ |
| Publicaciones, imágenes, búsqueda, ofertas, pagos, liberación, anti-evasión, Admin API, modos, canal manual | ✅ Fase 1 |
| **Motor de conversación**: webhook WhatsApp → cola durable → worker → intención → máquina de estados → Dolphin → herramientas | ✅ Fase 2 |
| **Cliente WhatsApp Cloud API**: texto, imagen, plantilla, marcar leído, descarga de media, reintentos, idempotencia | ✅ Fase 2 |
| **Ventana de 24h**: sesión gratis dentro de la ventana; fuera, una plantilla aprobada y el resto espera | ✅ Fase 2 |
| **Outbox** despachado por el worker (at-least-once, backoff, `SKIP LOCKED`, estados de entrega de Meta) | ✅ Fase 2 |
| **Mercado Pago Checkout Pro** real (preferencia ARS 3000, webhook `payment` + `merchant_order`, validaciones) | ✅ Fase 2 |
| **Cifrado a nivel campo** de teléfonos, nombres y mensajes sensibles (AES-256-GCM + índice ciego) | ✅ Fase 2 |
| **Workers recuperables**: estado por job, historial, heartbeats, recuperación al reiniciar, advisory locks, política por modo | ✅ Fase 3 |
| **Outbox sin duplicados** (claim → envío → settle, confirmación por `biz_opaque_callback_data`, recuperación de envíos en vuelo) | ✅ Fase 3 |
| **Bus de eventos interno** persistido (handlers críticos / best effort con reintentos) | ✅ Fase 3 |
| **Canal manual**: puntaje 0-100 anti-abuso, diversidad, control de exposición, sugerencias, registro, bajas, reconciliación | ✅ Fase 3 |
| **Modos** aplicados en workers (plazos congelados en PAUSED/EMERGENCY y extendidos al volver) | ✅ Fase 3 |
| **Backups** `pg_dump` con snapshot consistente, checksum, manifiesto, rotación, restauración verificada | ✅ Fase 3 |
| **Métricas** para el dashboard (`GET /admin/metrics`) | ✅ Fase 3 |
| Pruebas con credenciales sandbox reales de Meta / Mercado Pago | ⏳ requiere credenciales del dueño |
| Consola Android de administración | ⏳ Fase 4 |
| Instalación del servidor Dolphin | ⏳ cuando exista el servidor físico |

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
  app/workers/      runtime recuperable (jobs, heartbeats, recuperación) + scheduler
  migrations/       Alembic
  tests/            unit / integration / security / e2e
dolphin/            prompts, políticas y definición de herramientas (montado como volumen)
docker/             Dockerfile del backend, nginx de referencia
scripts/            create_admin, backup/restore de PostgreSQL, tests, export de herramientas
android/admin-console/  (fase 4)
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

## Mensajería WhatsApp (fase 2)

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
(ver `docker/nginx.conf`). Backups: ver [Backups y recuperación ante desastres](#backups-y-recuperación-ante-desastres-sección-45).

### Dolphin (cuando esté el servidor)

Servir el modelo con cualquier servidor OpenAI-compatible con *tool calling* habilitado, por ejemplo vLLM:

```bash
vllm serve dphn/Dolphin-Mistral-24B-Venice-Edition --enable-auto-tool-choice --tool-call-parser mistral
```

y configurar `DOLPHIN_BASE_URL=http://<servidor>:8000/v1`. Prompts y políticas se editan en `dolphin/` sin
tocar código.

## Tests

Corren contra **PostgreSQL y Redis reales** (la base de tests se borra y se recrea; la migración se prueba con
upgrade → downgrade → upgrade al inicio, y la `0002` y la `0003` con datos reales ida y vuelta).

Qué es real y qué es simulado en los tests:

* **Real:** PostgreSQL, Redis, migraciones, toda la lógica de dominio, la API HTTP (TestClient), la verificación de
  firmas de los webhooks de WhatsApp y Mercado Pago (payloads con la forma real de Meta/MP, firmados), la cola de
  entrada, el outbox, la máquina de estados, el cifrado, la construcción de requests HTTP de los clientes de
  WhatsApp y Mercado Pago (verificada con `httpx.MockTransport`: URLs, headers, cuerpos, reintentos, errores).
* **Real (fase 3):** el proceso worker (`python -m app.workers.scheduler` lanzado como subproceso: recuperación al
  arrancar, caída con SIGKILL, reinicio y parada con SIGTERM), advisory locks, `pg_dump`/`pg_restore` reales,
  escrituras concurrentes durante el backup, caídas simuladas a mitad de un envío de WhatsApp.
* **Simulado:** el modelo Dolphin (`MockDolphinClient` con guiones por turno), la red hacia Meta
  (`RecordingWhatsAppClient`) y hacia Mercado Pago (`FakeGateway`). No se usaron credenciales reales. El backup en
  modo docker no se probó (no hay Docker en el entorno de desarrollo).
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
3. **Fase 3 ✅:** workers recuperables, outbox sin duplicados, bus de eventos, canal manual completo (puntaje,
   diversidad, exposición, reconciliación), modos en workers, backups/restauración, métricas.
4. **Fase 4:** consola Android de administración.
5. **Fase 5:** servidor Dolphin físico, ajuste de prompts con conversaciones reales.

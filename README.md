# PERMUTADOS

Marketplace de compra/venta/permuta que funciona **dentro de WhatsApp**. Un bot 1:1 (WhatsApp Cloud API)
conversa con el usuario mediante un LLM local (**Dolphin-Mistral-24B-Venice-Edition**, servido con una API
compatible con OpenAI). El backend es la única fuente de verdad: Dolphin solo puede *pedir* acciones a través
de una lista cerrada de herramientas; los permisos, estados, pagos y la liberación de contactos los decide el
backend.

> Repositorio privado. Todos los derechos reservados (ver `LICENSE`).

## Estado: Fase 1 (backend)

| Área | Estado |
|---|---|
| Modelo de datos completo (23 tablas, enums, constraints, triggers, índices parciales, FTS español) | ✅ |
| Migración Alembic `0001` (upgrade / downgrade / `alembic check` sin drift) | ✅ |
| Publicaciones (borrador con foto → identificación IA → datos → preview → publicar; editar, borrar, vencimiento 30d) | ✅ |
| Validación estricta de imágenes (magic bytes, tamaño, decodificación, bombas, resolución, calidad) | ✅ |
| Búsqueda (FTS + filtros, 5 resultados por página, "más", selección por número/ordinal/código/texto) | ✅ |
| Ofertas (pausa del artículo, 24h para responder, aviso de 24h a ambas partes, arrepentimiento del dueño hasta el pago) | ✅ |
| Pagos (fee ARS 3000 configurable, ventana de 24h, webhook firmado, idempotencia, conciliación, reembolsos marcados) | ✅ |
| Liberación de contacto (WhatsApp de ambos + palabra clave única tipo `LUNA-7428`) | ✅ |
| Anti-evasión (1er intento: advertencia explícita; 2do: suspensión pendiente de revisión del admin) | ✅ |
| Capa Dolphin (cliente OpenAI-compatible, mock determinístico, 20 herramientas permitidas, filtros) | ✅ |
| Admin API (JWT access+refresh con rotación y detección de reuso, RBAC, rate limiting, auditoría) | ✅ |
| Modos de sistema ONLINE / PAUSED / EMERGENCY | ✅ |
| Canal de WhatsApp: puntaje de atractivo, sugerencias y registro **manual** de posts | ✅ |
| WhatsApp / Mercado Pago: interfaces + clientes HTTP + webhooks con verificación de firma | ✅ (sin credenciales reales) |
| Procesamiento asíncrono de mensajes entrantes de WhatsApp (webhook → cola → conversación) | ⏳ Fase 2 |
| Instalación del servidor Dolphin | ⏳ cuando exista el servidor físico |
| Consola Android de administración | ⏳ Fase 3 |

## Arquitectura

```
WhatsApp Cloud API ──► /webhooks/whatsapp ──► conversation service ──► Dolphin (OpenAI-compatible)
                                                      │                     │ tool calls (allowlist)
                                                      ▼                     ▼
                                              domain services ◄──── tools.execute (validación estricta)
                                     (publications, search, offers, payments, security, channel)
                                                      │
Mercado Pago ──► /webhooks/mercadopago ──► payments ──┤──► PostgreSQL (fuente de verdad, auditoría)
                                                      └──► Redis (rate limiting)
Admin (Android, fase 3) ──► /admin/* (JWT + RBAC)       Worker (APScheduler + advisory locks): vencimientos,
                                                         conciliación de pagos, canal
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

## Desarrollo local (sin Docker)

Requisitos: Python 3.13, PostgreSQL 17, Redis 7+.

```bash
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements-dev.txt
cp .env.example .env    # completar
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
upgrade → downgrade → upgrade al inicio). Dolphin, Mercado Pago y el envío por WhatsApp se simulan con dobles
determinísticos.

```bash
cd backend && ../.venv/bin/pytest -q
# o: scripts/run_tests.sh   (ruff + pytest)
# variables: TEST_DATABASE_URL, TEST_REDIS_URL
```

## Plan de fases

1. **Fase 1 (este repo):** backend, modelo, servicios, capa Dolphin, Admin API, webhooks, tests.
2. **Fase 2:** procesamiento real de mensajes WhatsApp (cola + worker + envío de notificaciones del outbox),
   media download, pruebas con credenciales sandbox de Meta y Mercado Pago, cifrado de teléfonos a nivel campo.
3. **Fase 3:** consola Android de administración.
4. **Fase 4:** servidor Dolphin físico, ajuste de prompts con conversaciones reales.

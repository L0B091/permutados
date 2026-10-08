"""User-facing texts (Spanish, Argentina). Deterministic: critical messages are never written by the AI.

Every offer-related message states the expiry window explicitly (both parties know from the
start that an offer expires after ``offer_expiration_hours`` = 24h by default).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

PHOTO_REQUEST = (
    "Enviá una sola foto del artículo. La imagen debe ser clara y tener buena calidad para poder "
    "identificarlo correctamente."
)
IMAGE_REJECTED = (
    "La imagen no tiene suficiente calidad para identificar correctamente el artículo. Por favor, "
    "enviá otra foto más clara."
)
ONLY_ONE_IMAGE = "Por ahora cada publicación admite una sola foto. Enviá únicamente la foto principal del artículo."
SYSTEM_BLOCKED = "Permutados está temporalmente en mantenimiento. Probá de nuevo en un rato."
ACCOUNT_SUSPENDED = (
    "Tu cuenta está suspendida y en revisión por un administrador. Te vamos a avisar cuando haya una decisión."
)


def money(amount: Decimal | int | float | str, currency: str = "ARS") -> str:
    d = Decimal(str(amount)).quantize(Decimal("0.01"))
    whole, frac = f"{d:.2f}".split(".")
    sign = "-" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    grouped = ".".join([whole[max(i - 3, 0) : i] for i in range(len(whole), 0, -3)][::-1])
    text = f"{currency} {sign}{grouped}"
    return text if frac == "00" else f"{text},{frac}"


def local_time(dt: datetime) -> str:
    return dt.astimezone(AR_TZ).strftime("%d/%m %H:%M") + " (hora Argentina)"


def offer_summary(offer_type: str, price: Decimal | None, offered_item: str | None, currency: str = "ARS") -> str:
    if offer_type == "MONEY":
        return f"{money(price, currency)}"  # type: ignore[arg-type]
    if offer_type == "SWAP":
        return f"permuta por: {offered_item}"
    return f"{money(price, currency)} + permuta por: {offered_item}"  # type: ignore[arg-type]


# ---- offers -----------------------------------------------------------------------------------
def offer_created_bidder(*, item_code: str, title: str, summary: str, hours: int, expires_at: datetime) -> str:
    return (
        f"✅ Tu oferta por {title} ({item_code}) fue enviada: {summary}.\n"
        f"⏳ Tu oferta vence en {hours}h (el {local_time(expires_at)}). Si el propietario no responde antes, "
        f"caduca automáticamente y no se cobra nada.\n"
        f"Podés cancelarla mientras no haya sido aceptada."
    )


def offer_received_owner(
    *, item_code: str, title: str, summary: str, message: str | None, hours: int, expires_at: datetime
) -> str:
    extra = f"\nMensaje del ofertante: “{message}”" if message else ""
    return (
        f"📩 Recibiste una oferta por tu {title} ({item_code}): {summary}.{extra}\n"
        f"⏳ Tenés {hours}h para aceptar o rechazar, luego la oferta caduca (vence el {local_time(expires_at)}).\n"
        f"Mientras tanto tu publicación queda pausada y no recibe otras ofertas.\n"
        f"Respondé ACEPTAR o RECHAZAR."
    )


def offer_accepted_bidder(*, item_code: str, title: str, fee: str, hours: int, due_at: datetime) -> str:
    return (
        f"🎉 ¡El propietario aceptó tu oferta por {title} ({item_code})!\n"
        f"Para recibir su WhatsApp tenés que pagar la tarifa de liberación de contacto: {fee}.\n"
        f"Tenés {hours}h para pagar (hasta el {local_time(due_at)}); si no, la operación se cancela.\n"
        f"Respondé PAGAR y te mando el link oficial de Mercado Pago."
    )


def offer_accepted_owner(*, item_code: str, title: str, hours: int) -> str:
    return (
        f"Aceptaste la oferta por {title} ({item_code}). El ofertante tiene {hours}h para pagar la tarifa. "
        f"Vos no pagás nada. Podés arrepentirte mientras el ofertante todavía no haya pagado."
    )


def offer_rejected_bidder(*, item_code: str, title: str) -> str:
    return f"El propietario rechazó tu oferta por {title} ({item_code}). Podés buscar otros artículos cuando quieras."


def offer_expired_bidder(*, item_code: str, title: str, hours: int) -> str:
    return (
        f"⌛ Tu oferta por {title} ({item_code}) caducó: el propietario no respondió dentro de las {hours}h. "
        f"No se cobró nada."
    )


def offer_expired_owner(*, item_code: str, title: str, hours: int) -> str:
    return (
        f"⌛ La oferta por tu {title} ({item_code}) caducó porque pasaron {hours}h sin respuesta. "
        f"Tu publicación vuelve a estar activa."
    )


def offer_cancelled_owner(*, item_code: str, title: str) -> str:
    return f"El ofertante canceló su oferta por tu {title} ({item_code}). Tu publicación vuelve a estar activa."


def offer_withdrawn_bidder(*, item_code: str, title: str) -> str:
    return f"El propietario se arrepintió de la operación por {title} ({item_code}) antes de tu pago. No se cobró nada."


def payment_timeout_bidder(*, item_code: str, title: str) -> str:
    return (
        f"La operación por {title} ({item_code}) se canceló porque no se registró el pago a tiempo. No se cobró nada."
    )


def payment_timeout_owner(*, item_code: str, title: str) -> str:
    return (
        f"La operación por tu {title} ({item_code}) se canceló porque el ofertante no pagó a tiempo. "
        f"Tu publicación vuelve a estar activa."
    )


def item_unavailable_bidder(*, item_code: str) -> str:
    return f"El artículo {item_code} ya no está disponible, así que tu oferta se cerró. No se cobró nada."


# ---- payment / release -----------------------------------------------------------------------
def payment_link(*, fee: str, url: str) -> str:
    return f"Pagá la tarifa de liberación ({fee}) desde este link oficial de Mercado Pago:\n{url}"


def contact_released_bidder(*, item_code: str, title: str, owner_phone: str, keyword: str) -> str:
    return (
        f"✅ Pago confirmado por Mercado Pago. Operación {title} ({item_code}).\n"
        f"WhatsApp del propietario: +{owner_phone}\n"
        f"🔐 Palabra clave de seguridad: {keyword}\n"
        f"Pedile al propietario que te diga la misma palabra clave antes de coordinar. "
        f"Desde acá Permutados deja de intervenir. ¡Éxitos!"
    )


def contact_released_owner(*, item_code: str, title: str, bidder_phone: str, keyword: str) -> str:
    return (
        f"✅ El ofertante pagó la tarifa. Operación {title} ({item_code}).\n"
        f"WhatsApp del ofertante: +{bidder_phone}\n"
        f"🔐 Palabra clave de seguridad: {keyword}\n"
        f"Verificá que el ofertante te diga la misma palabra clave. "
        f"Tu publicación queda cerrada y Permutados deja de intervenir."
    )


# ---- anti-evasion ------------------------------------------------------------------------------
EVASION_WARNING = (
    "⚠️ Advertencia: no está permitido compartir teléfonos, emails, redes sociales, links ni "
    "intentar contactarse por fuera de Permutados. Tu mensaje no fue enviado.\n"
    "La próxima vez que intentes hacerlo, tu cuenta será suspendida y quedará en revisión por un "
    "administrador."
)
EVASION_SUSPENDED = (
    "⛔ Volviste a intentar compartir datos de contacto por fuera de Permutados. Tu cuenta quedó "
    "suspendida y en revisión por un administrador."
)


# ---- publications ------------------------------------------------------------------------------
def publication_published(*, item_code: str, title: str, days: int) -> str:
    return f"🟢 Tu publicación {title} quedó activa con el código {item_code}. Vence en {days} días."


def publication_expiry_warning(*, item_code: str, title: str, expires_at: datetime) -> str:
    return f"Tu publicación {title} ({item_code}) vence el {local_time(expires_at)}."


def publication_expired(*, item_code: str, title: str) -> str:
    return f"Tu publicación {title} ({item_code}) venció y ya no se muestra. Podés publicarla de nuevo."


# ---- inbound edge cases ----------------------------------------------------------------------------
UNSUPPORTED_MESSAGE = (
    "Por ahora entiendo mensajes de texto y fotos. Contame por escrito qué querés hacer: publicar, buscar, "
    "ofertar o ver tus operaciones."
)
IMAGE_DOWNLOAD_FAILED = "No pude descargar tu foto. ¿Me la mandás de nuevo, por favor?"
LOCATION_NOT_ALLOWED = (
    "Por seguridad no usamos ubicaciones exactas. Decime solo tu barrio o localidad (por ejemplo: Palermo, Quilmes)."
)

# ---- PII-free summaries for the paid fallback template (outside the 24h window) -------------------
# Sent as the single {{1}} parameter of WHATSAPP_FALLBACK_TEMPLATE. Never include phones, names or links.
_SUMMARIES = {
    "REPLY": "tenés una respuesta pendiente",
    "OFFER_CREATED_BIDDER": "tu oferta fue enviada y vence en 24h",
    "OFFER_RECEIVED_OWNER": "recibiste una oferta: tenés 24h para aceptar o rechazar",
    "OFFER_ACCEPTED_BIDDER": "aceptaron tu oferta: tenés 24h para pagar la tarifa",
    "OFFER_ACCEPTED_OWNER": "aceptaste una oferta, esperamos el pago del ofertante",
    "OFFER_REJECTED_BIDDER": "rechazaron tu oferta",
    "OFFER_EXPIRED_BIDDER": "tu oferta venció",
    "OFFER_EXPIRED_OWNER": "una oferta que recibiste venció",
    "OFFER_CANCELLED_OWNER": "el ofertante canceló su oferta",
    "OFFER_WITHDRAWN_BIDDER": "el propietario se arrepintió de la operación",
    "ITEM_UNAVAILABLE_BIDDER": "un artículo de tu oferta ya no está disponible",
    "OFFER_CLOSED_COUNTERPART_BLOCKED": "una operación tuya fue cerrada",
    "PAYMENT_TIMEOUT_BIDDER": "venció el plazo para pagar una operación",
    "PAYMENT_TIMEOUT_OWNER": "el ofertante no pagó a tiempo, tu publicación volvió a estar activa",
    "CONTACT_RELEASED_BIDDER": "el pago fue confirmado y tus datos de contacto están listos",
    "CONTACT_RELEASED_OWNER": "el ofertante pagó y los datos de contacto están listos",
    "PUBLICATION_EXPIRY_WARNING": "una publicación tuya está por vencer",
    "PUBLICATION_EXPIRED": "una publicación tuya venció",
}


def summary_for(kind: str) -> str:
    return _SUMMARIES.get(kind, "tenés una novedad en Permutados")

import pytest

from app.services.conversation.intents import Intent, detect_intent


@pytest.mark.parametrize(
    ("text", "state", "expected"),
    [
        ("busco una bicicleta rodado 29", "IDLE", Intent.SEARCH),
        ("quiero vender mi taladro", "IDLE", Intent.PUBLISH),
        ("más", "BROWSING_RESULTS", Intent.NEXT_PAGE),
        ("ver más", "BROWSING_RESULTS", Intent.NEXT_PAGE),
        ("el 2", "BROWSING_RESULTS", Intent.SELECT),
        ("el segundo", "BROWSING_RESULTS", Intent.SELECT),
        ("me interesa ITEM-ABCDE", "IDLE", Intent.SELECT),
        ("ofrezco 100 mil", "ITEM_SELECTED", Intent.OFFER),
        ("te lo cambio por una play 4", "ITEM_SELECTED", Intent.OFFER),
        ("acepto", "OWNER_DECIDING", Intent.ACCEPT_OFFER),
        ("ACEPTAR", "OWNER_DECIDING", Intent.ACCEPT_OFFER),
        ("rechazar", "OWNER_DECIDING", Intent.REJECT_OFFER),
        ("no acepto la oferta", "OWNER_DECIDING", Intent.REJECT_OFFER),
        ("PAGAR", "PAYMENT_PENDING", Intent.PAY),
        ("ya pagué", "PAYMENT_PENDING", Intent.PAYMENT_STATUS),
        ("cancelar mi oferta", "OFFER_PENDING", Intent.CANCEL),
        ("sí", "PUBLISHING_PREVIEW", Intent.CONFIRM),
        ("dale publicala", "PUBLISHING_PREVIEW", Intent.CONFIRM),
        ("no", "PUBLISHING_PREVIEW", Intent.DENY),
        ("mis publicaciones", "IDLE", Intent.MY_PUBLICATIONS),
        ("quiero cambiar el precio", "IDLE", Intent.EDIT_PUBLICATION),
        ("borrar mi publicacion", "IDLE", Intent.DELETE_PUBLICATION),
        ("quiero denunciar este artículo", "IDLE", Intent.REPORT),
        ("hola", "IDLE", Intent.GREETING),
        ("¿cómo funciona?", "IDLE", Intent.HELP),
        ("asdkjh", "IDLE", Intent.UNKNOWN),
        ("", "IDLE", Intent.UNKNOWN),
    ],
)
def test_detect_intent(text, state, expected):
    assert detect_intent(text, state=state) == expected


def test_image_means_publish():
    assert detect_intent(None, has_image=True, state="IDLE") == Intent.PUBLISH


def test_confirm_needs_context():
    # "sí" with nothing pending is not a confirmation of anything
    assert detect_intent("sí", state="IDLE") != Intent.CONFIRM

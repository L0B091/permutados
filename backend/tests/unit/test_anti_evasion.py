import pytest

from app.services.security.anti_evasion import detect

POSITIVES = [
    ("llamame al 11 5555-4444", "PHONE"),
    ("mi cel es 1155554444", "PHONE"),
    ("+54 9 11 2345-6789", "PHONE"),
    ("escribime a juan.perez@gmail.com", "EMAIL"),
    ("juan arroba gmail punto com", "EMAIL"),
    ("te paso mi telegram", "TELEGRAM"),
    ("seguime en instagram", "INSTAGRAM"),
    ("mi ig: @juancho_ventas", "SOCIAL_HANDLE"),
    ("buscame en facebook", "FACEBOOK"),
    ("www.mercadolibre.com.ar/algo", "URL"),
    ("https://bit.ly/abc", "URL"),
    ("hablemos por fuera así nos ahorramos la comision", "EVASION_PHRASE"),
    ("pasame tu numero", "EVASION_PHRASE"),
    ("mandame wsp", "EVASION_PHRASE"),
    ("once cinco cinco cinco cinco cuatro cuatro cuatro cuatro", "PHONE_SPELLED"),
    ("tengo tiktok", "OTHER_NETWORK"),
]

NEGATIVES = [
    "te ofrezco $150.000",
    "te doy 1.500.000 pesos",
    "vale 150 mil",
    "iPhone 13 128gb impecable",
    "talle 42, mide 120x60x75 cm",
    "Samsung A52 del 2021",
    "precio 12000000",
    "modelos 2019 2020 2021",
    "funciona perfecto, tiene 2 años de uso",
    "te doy 85000 y una bici",
    "acepto permuta por una play 5",
    "ofrezco 2500000",
    "es un sillón de 3 cuerpos",
    "lo uso desde 2018, 64 gb",
    "hola! me interesa mucho, ¿lo podés guardar?",
    "busco auriculares bluetooth en Palermo hasta 50 mil",
]


@pytest.mark.parametrize("text,kind", POSITIVES)
def test_detects_contact_attempts(text, kind):
    r = detect(text)
    assert r.detected and kind in r.kinds, (text, r.kinds)


@pytest.mark.parametrize("text", NEGATIVES)
def test_no_false_positives_on_prices_and_specs(text):
    assert not detect(text).detected, (text, detect(text).kinds)

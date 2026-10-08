"""Contract between the Android Admin Console and the backend.

Parses the Retrofit interfaces (AdminApi.kt / AuthApi.kt) and the request DTOs (Models.kt) of the app and checks
them against the real FastAPI application: every endpoint the app calls must exist with that HTTP method, every
query parameter must be accepted, and every request body must match the Pydantic model (no unknown keys, all
required keys sent). If someone renames a backend route, this fails instead of the console breaking at runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "android" / "admin-console" / "app" / "src" / "main" / "java" / "com" / "permutados" / "admin"
API_FILES = [SRC / "data" / "remote" / "AdminApi.kt", SRC / "data" / "remote" / "AuthApi.kt"]
MODELS = SRC / "domain" / "model" / "Models.kt"

CALL = re.compile(r'@(GET|POST|PUT|PATCH|DELETE)\("([^"]+)"\)')


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", "/" + path.lstrip("/"))


def _app_calls() -> list[dict]:
    calls = []
    for f in API_FILES:
        text = f.read_text()
        matches = list(CALL.finditer(text))
        for i, m in enumerate(matches):
            chunk = text[m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)]
            body = re.search(r"@Body\s+\w+:\s*(\w+)", chunk)
            calls.append(
                {
                    "file": f.name,
                    "method": m.group(1),
                    "path": _norm(m.group(2)),
                    "query": re.findall(r'@Query\("([^"]+)"\)', chunk),
                    "body": body.group(1) if body else None,
                }
            )
    return calls


def _kotlin_fields(class_name: str) -> list[tuple[str, bool]]:
    """(json_name, has_default) for each constructor property of a Kotlin data class."""
    text = MODELS.read_text()
    start = text.index(f"data class {class_name}(") + len(f"data class {class_name}(")
    depth, i = 1, start
    while depth:
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        i += 1
    params = text[start : i - 1]
    out = []
    for m in re.finditer(r'(?:@SerialName\("([^"]+)"\)\s*)?val\s+(\w+)\s*:\s*[^,=]+(=)?', params):
        out.append((m.group(1) or m.group(2), m.group(3) is not None))
    return out


@pytest.fixture(scope="module")
def openapi():
    from app.main import create_app

    return create_app().openapi()


@pytest.fixture(scope="module")
def backend_routes(openapi):
    routes = {}
    for path, ops in openapi["paths"].items():
        for method, op in ops.items():
            routes[(method.upper(), _norm(path))] = op
    return routes


def test_android_sources_present():
    for f in [*API_FILES, MODELS]:
        assert f.exists(), f
    assert len(_app_calls()) >= 45


def test_every_app_endpoint_exists_in_backend(backend_routes):
    missing = [
        f"{c['method']} {c['path']} ({c['file']})"
        for c in _app_calls()
        if (c["method"], c["path"]) not in backend_routes
    ]
    assert not missing, "Android calls endpoints the backend does not expose: " + ", ".join(missing)


def test_query_parameters_are_accepted(backend_routes):
    problems = []
    for c in _app_calls():
        op = backend_routes.get((c["method"], c["path"]))
        if not op:
            continue
        accepted = {p["name"] for p in op.get("parameters", []) if p["in"] == "query"}
        for q in c["query"]:
            if q not in accepted:
                problems.append(f"{c['method']} {c['path']}?{q}")
    assert not problems, problems


def test_request_bodies_match_pydantic_models(backend_routes, openapi):
    schemas = openapi["components"]["schemas"]
    problems, checked = [], 0
    for c in _app_calls():
        op = backend_routes.get((c["method"], c["path"]))
        if not op or not c["body"] or c["body"] == "JsonObject":
            continue
        ref = op["requestBody"]["content"]["application/json"]["schema"].get("$ref")
        assert ref, f"{c['path']} has no typed body in the backend"
        schema = schemas[ref.split("/")[-1]]
        props, required = set(schema.get("properties", {})), set(schema.get("required", []))
        fields = _kotlin_fields(c["body"])
        names = {n for n, _ in fields}
        if names - props:
            problems.append(f"{c['body']} sends unknown keys {sorted(names - props)} to {c['path']}")
        if required - names:
            problems.append(f"{c['body']} misses required keys {sorted(required - names)} for {c['path']}")
        checked += 1
    assert checked >= 10
    assert not problems, problems


def test_bodyless_posts_need_no_body(backend_routes):
    for c in _app_calls():
        op = backend_routes.get((c["method"], c["path"]))
        if op and c["method"] in {"POST", "PUT", "PATCH"} and not c["body"]:
            assert "requestBody" not in op or not op["requestBody"].get("required"), c


def test_app_contains_no_third_party_credentials():
    """The console only talks to the backend: no WhatsApp/Mercado Pago/Dolphin/DB secrets or URLs in its code."""
    app_root = ROOT / "android" / "admin-console"
    forbidden = re.compile(
        r"(graph\.facebook\.com|api\.mercadopago\.com|APP_USR-|TEST-\d{6,}|postgresql(\+psycopg)?://|redis://|"
        r"WHATSAPP_ACCESS_TOKEN|MERCADOPAGO_ACCESS_TOKEN|DOLPHIN_API_KEY|ENCRYPTION_KEY|EAA[A-Za-z0-9]{20,})"
    )
    hits = []
    for f in app_root.rglob("*"):
        if (
            f.is_file()
            and f.suffix in {".kt", ".kts", ".xml", ".properties", ".toml", ".pro"}
            and "build" not in f.parts
        ):
            for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if forbidden.search(line):
                    hits.append(f"{f.relative_to(app_root)}:{n}")
    assert not hits, hits

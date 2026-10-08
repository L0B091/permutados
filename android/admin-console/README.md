# PERMUTADOS — Consola de administración Android

App nativa (Kotlin + Jetpack Compose) para administrar PERMUTADOS. **Solo habla con la API admin del backend**
(`/admin/...`) usando el JWT del administrador. No contiene credenciales de WhatsApp, Mercado Pago, Dolphin ni de
la base de datos (lo verifica el test `backend/tests/unit/test_android_contract.py::test_app_contains_no_third_party_credentials`).

## Pantallas (sección 8 de la especificación)

| Sección | Qué hace | Endpoints |
|---|---|---|
| Login | URL del backend + usuario/contraseña. Sesión cifrada con Android Keystore. | `POST /admin/auth/login`, `/refresh`, `/logout` |
| Dashboard | Contadores + métricas (1/7/30/90 días): usuarios, publicaciones, búsquedas, ofertas, pagos, liberaciones, conversión, canal, errores, baneos, rendimiento, serie diaria | `GET /admin/dashboard`, `GET /admin/metrics` |
| Usuarios | Búsqueda por WhatsApp exacto (índice ciego), filtro por estado, perfil (advertencias, baneos, publicaciones, ofertas, reportes), suspender / confirmar baneo / rehabilitar | `/admin/users…` |
| Publicaciones | Lista/búsqueda, detalle, edición de textos con motivo (el backend rechaza datos de contacto), eliminar, destacar, puntaje del Canal | `/admin/items…` |
| Ofertas | Lista por estado | `GET /admin/offers` |
| Operaciones | Lista y detalle (pagos, notificaciones de liberación; la palabra clave nunca se muestra) | `/admin/transactions…` |
| Pagos | Lista por estado (incluye `NEEDS_REFUND`, sandbox/producción) | `GET /admin/payments` |
| Conversaciones | Solo MODERATOR+. Lista con metadatos; abrir una conversación queda auditado | `/admin/conversations…` |
| Seguridad | Baneos pendientes de revisión, advertencias, reportes (resolver), eventos, auditoría (ADMIN) | `/admin/security/*`, `/admin/reports`, `/admin/audit` |
| Canal | Administración **manual**: resumen/cuota, sugerencias → encolar, registrar publicación hecha a mano, publicada/fallida/reintentar, solicitar baja, confirmar eliminada, bajas pendientes, reconciliar, recalcular puntajes, quitar destacados | `/admin/channel/*`, `/admin/items/{id}/feature` |
| Configuración | Precio por liberación, moneda, vencimientos, límites, parámetros del Canal (ADMIN edita), modo del sistema ONLINE/PAUSED/EMERGENCY con motivo | `/admin/config`, `/admin/system/mode` |
| Sistema | Estado de PostgreSQL, Redis, Dolphin, WhatsApp, Mercado Pago, workers, jobs del scheduler y ejecuciones recientes | `/admin/system/health`, `/admin/workers` |

Las 49 operaciones admin del backend están cubiertas por la app; el test de contrato del backend falla si la app
llama a una ruta, parámetro o cuerpo que el backend no acepta.

## Arquitectura

```
ui/            Compose (screens) + ViewModels (StateFlow<UiState>) — MVVM
domain/        modelos (@Serializable, sin tipos Android) + interfaces de repositorios
data/remote    Retrofit (AdminApi, AuthApi), OkHttp, AuthInterceptor, TokenAuthenticator, ErrorMapper
data/session   Session, DataStoreSessionStore (cifrado), KeystoreTokenCipher (AES-256-GCM en Android Keystore)
data/repository implementaciones de los repositorios
AppContainer   inyección manual de dependencias
```

Seguridad:
- **Solo HTTPS** en release (`network_security_config` + validación de la URL + OkHttp `MODERN_TLS`). En debug se
  permite HTTP únicamente hacia `10.0.2.2` / `localhost` / `127.0.0.1` (backend local).
- Tokens cifrados con una clave AES-256-GCM no exportable del Android Keystore y guardados en DataStore. Si la
  clave no sirve (reinstalación, restauración en otro equipo) la sesión se descarta.
- Refresh token rotado de a una sola vez aunque haya varias peticiones con 401 simultáneas (el backend revoca la
  familia si detecta reutilización). Si el refresh es rechazado se vuelve al login.
- El bearer solo se envía al host de la sesión. Sin interceptor de logging: tokens y cuerpos nunca van a logcat.
  `toString()` de credenciales/tokens está redactado.
- `allowBackup=false`, reglas de extracción vacías, `FLAG_SECURE` (sin capturas ni miniatura en recientes).
- La app solo **oculta** acciones según el rol; la autoridad es el backend (RBAC VIEWER < MODERATOR < ADMIN).

## Requisitos

- JDK 17, Android SDK con `platforms;android-34` y `build-tools;34.0.0` (`local.properties` con `sdk.dir=...`,
  no versionado).
- Versiones fijadas en `gradle/libs.versions.toml` (AGP 8.5.2, Kotlin 2.0.21, Compose BOM 2024.09.03,
  Retrofit 2.11.0, OkHttp 4.12.0, DataStore 1.1.1, Robolectric 4.13). Gradle 8.7 vía wrapper.

## Compilar y probar

```bash
cd android/admin-console
./gradlew assembleDebug            # app/build/outputs/apk/debug/app-debug.apk
./gradlew testDebugUnitTest        # JVM + Robolectric (45 tests)
./gradlew lintDebug                # 0 errores
./gradlew assembleDebugAndroidTest # compila los tests instrumentados
./gradlew connectedDebugAndroidTest  # requiere TU dispositivo/emulador (Keystore real)
```

URL por defecto del backend: propiedad Gradle `permutados.apiBaseUrl` (en `gradle.properties` o
`-Ppermutados.apiBaseUrl=https://api.tu-dominio/`). También se puede escribir en la pantalla de login.

### APK de release firmado

1. Crear un keystore **fuera del repo** (nunca se versiona; `.gitignore` excluye `*.jks`, `*.keystore` y
   `keystore.properties`):
   `keytool -genkeypair -v -keystore ~/keys/permutados-admin.jks -alias permutados -keyalg RSA -keysize 4096 -validity 10000`
2. Crear `android/admin-console/keystore.properties`:
   ```
   storeFile=/home/USUARIO/keys/permutados-admin.jks
   storePassword=...
   keyAlias=permutados
   keyPassword=...
   ```
3. `./gradlew assembleRelease -Ppermutados.apiBaseUrl=https://api.tu-dominio/` →
   `app/build/outputs/apk/release/app-release.apk` (R8 + shrink de recursos). Sin `keystore.properties` se genera
   `app-release-unsigned.apk`, que hay que firmar con `apksigner`.
4. Guardar el keystore y sus contraseñas en un lugar seguro: sin él no se pueden publicar actualizaciones.

## Qué se ejecutó en la máquina de construcción (2026-10-08)

- `assembleDebug`, `assembleDebugAndroidTest`, `assembleRelease` (sin firmar, con R8): **OK**.
- `testDebugUnitTest`: **45/45 OK** (validación de URL, roles, mapeo de errores, red real contra MockWebServer
  incluyendo rotación del refresh y 401 concurrentes, almacenamiento cifrado en DataStore, ViewModels con fakes,
  UI de login en Compose con Robolectric).
- `lintDebug`: 0 errores (39 avisos: versiones más nuevas disponibles y checks de navigation omitidos).
- **No se ejecutaron** los tests instrumentados (`KeystoreTokenCipherTest`) ni se instaló la app en un
  dispositivo: el único emulador de esa máquina pertenece a otro proyecto y no se tocó. Ejecutalos en tu equipo con
  `connectedDebugAndroidTest`.
- El APK de release no está firmado (no hay keystore de producción, a propósito).

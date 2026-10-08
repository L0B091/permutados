package com.permutados.admin.core

/** Errors the console can show. `code` is the backend error code (e.g. CONTACT_DATA_IN_TEXT) when present. */
sealed class ApiError(open val code: String?, open val detail: String?) {
    data object Network : ApiError(null, null)
    data object Unauthorized : ApiError("UNAUTHORIZED", null)
    data object Forbidden : ApiError("FORBIDDEN", null)
    data object RateLimited : ApiError("RATE_LIMITED", null)
    data object Parsing : ApiError("PARSING", null)
    data class NotFound(override val code: String?, override val detail: String?) : ApiError(code, detail)
    data class Conflict(override val code: String?, override val detail: String?) : ApiError(code, detail)
    data class Validation(override val code: String?, override val detail: String?) : ApiError(code, detail)
    data class Server(val status: Int) : ApiError("HTTP_$status", null)
    data class Http(val status: Int, override val code: String?, override val detail: String?) : ApiError(code, detail)
    data class InvalidInput(override val detail: String) : ApiError("INVALID_INPUT", detail)

    /** Spanish message for the operator. Backend messages are appended when they exist (they never carry secrets). */
    val userMessage: String
        get() = when (this) {
            Network -> "Sin conexión con el servidor. Verificá la red y la URL del backend."
            Unauthorized -> "Sesión vencida o credenciales inválidas. Iniciá sesión nuevamente."
            Forbidden -> "Tu rol no tiene permiso para esta acción."
            RateLimited -> "Demasiados intentos. Esperá un minuto y reintentá."
            Parsing -> "Respuesta inesperada del servidor."
            is NotFound -> "No encontrado" + suffix()
            is Conflict -> "Estado no válido para esta acción" + suffix()
            is Validation -> "Datos inválidos" + suffix()
            is Server -> "Error del servidor ($status). Reintentá más tarde."
            is Http -> "Error HTTP $status" + suffix()
            is InvalidInput -> detail
        }

    private fun suffix(): String {
        val parts = listOfNotNull(code?.takeIf { it.isNotBlank() }, detail?.takeIf { it.isNotBlank() })
        return if (parts.isEmpty()) "." else ": " + parts.joinToString(" — ")
    }
}

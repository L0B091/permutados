package com.permutados.admin.core

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * Validates the backend base URL typed on the login screen.
 * - Release builds: HTTPS only.
 * - Debug builds: plain HTTP is accepted only for the developer machine (10.0.2.2 / localhost / 127.0.0.1),
 *   matching the debug network security config.
 * - No credentials, query or fragment in the URL. A trailing slash is added (Retrofit requirement).
 */
object BaseUrlValidator {
    private val LOCAL_HOSTS = setOf("10.0.2.2", "localhost", "127.0.0.1")

    fun normalize(raw: String, allowLocalCleartext: Boolean): Outcome<String> {
        val text = raw.trim()
        if (text.isEmpty()) return fail("Ingresá la URL del backend.")
        val url: HttpUrl = text.toHttpUrlOrNull() ?: return fail("URL inválida.")
        if (url.username.isNotEmpty() || url.password.isNotEmpty()) return fail("La URL no puede incluir credenciales.")
        if (url.query != null || url.fragment != null) return fail("La URL no puede incluir parámetros.")
        when (url.scheme) {
            "https" -> Unit
            "http" -> if (!(allowLocalCleartext && url.host in LOCAL_HOSTS)) {
                return fail("Solo se permite HTTPS.")
            }
            else -> return fail("Solo se permite HTTPS.")
        }
        val s = url.toString()
        return Outcome.Success(if (s.endsWith("/")) s else "$s/")
    }

    private fun fail(msg: String) = Outcome.Failure(ApiError.InvalidInput(msg))
}

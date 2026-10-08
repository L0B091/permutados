package com.permutados.admin.data.remote

import com.permutados.admin.data.session.SessionStore
import com.permutados.admin.domain.model.RefreshRequest
import kotlinx.coroutines.runBlocking
import okhttp3.Authenticator
import okhttp3.Request
import okhttp3.Response
import okhttp3.Route
import java.io.IOException

/**
 * On 401: rotates the refresh token once (POST /admin/auth/refresh) and retries the request with the new access
 * token. Serialized with a lock so concurrent 401s trigger a single refresh (the backend detects refresh-token
 * reuse and would revoke the whole family). If the refresh is rejected the session is cleared and the UI returns
 * to the login screen. Network errors during refresh keep the session (the call simply fails).
 */
class TokenAuthenticator(
    private val store: SessionStore,
    private val authApiFor: (String) -> AuthApi,
    private val nowEpochSec: () -> Long = { System.currentTimeMillis() / 1000 },
) : Authenticator {
    private val lock = Any()

    override fun authenticate(route: Route?, response: Response): Request? {
        val request = response.request
        if (isAuthEndpoint(request.url.encodedPath)) return null
        if (priorResponses(response) >= 2) {
            runBlocking { store.clear() }
            return null
        }
        val sentToken = request.header("Authorization")?.removePrefix("Bearer ")
        synchronized(lock) {
            val current = store.current() ?: return null
            if (sentToken != null && sentToken != current.accessToken) {
                return request.withBearer(current.accessToken) // another call refreshed meanwhile
            }
            val result = try {
                authApiFor(current.baseUrl).refresh(RefreshRequest(current.refreshToken)).execute()
            } catch (e: IOException) {
                return null
            }
            val body = result.body()
            if (!result.isSuccessful || body == null) {
                if (result.code() in 400..403) runBlocking { store.clear() }
                return null
            }
            val renewed = current.copy(
                accessToken = body.accessToken,
                refreshToken = body.refreshToken,
                role = body.role,
                accessExpiresAtEpochSec = nowEpochSec() + body.expiresIn,
            )
            runBlocking { store.save(renewed) }
            return request.withBearer(renewed.accessToken)
        }
    }

    private fun priorResponses(response: Response): Int {
        var n = 0
        var r = response.priorResponse
        while (r != null) {
            n++
            r = r.priorResponse
        }
        return n
    }
}

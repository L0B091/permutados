package com.permutados.admin.data.remote

import com.permutados.admin.data.session.SessionStore
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.Request
import okhttp3.Response

internal fun isAuthEndpoint(path: String) = path.endsWith("/admin/auth/login") || path.endsWith("/admin/auth/refresh")

internal fun Request.withBearer(token: String): Request = newBuilder().header("Authorization", "Bearer $token").build()

/** Adds the admin JWT. The token is only sent to the host of the session's backend (never to another origin)
 * and never to the login/refresh endpoints. Nothing is logged. */
class AuthInterceptor(private val store: SessionStore) : Interceptor {
    override fun intercept(chain: Interceptor.Chain): Response {
        val request = chain.request()
        val session = store.current()
        if (session == null || isAuthEndpoint(request.url.encodedPath) || request.header("Authorization") != null) {
            return chain.proceed(request)
        }
        val sessionUrl = session.baseUrl.toHttpUrlOrNull()
        val sameOrigin = sessionUrl != null && sessionUrl.scheme == request.url.scheme &&
            sessionUrl.host == request.url.host && sessionUrl.port == request.url.port
        return chain.proceed(if (sameOrigin) request.withBearer(session.accessToken) else request)
    }
}

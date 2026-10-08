package com.permutados.admin.data.repository

import com.permutados.admin.core.ApiError
import com.permutados.admin.core.BaseUrlValidator
import com.permutados.admin.core.Outcome
import com.permutados.admin.data.remote.ApiProvider
import com.permutados.admin.data.remote.safeCall
import com.permutados.admin.data.session.Session
import com.permutados.admin.data.session.SessionStore
import com.permutados.admin.domain.model.LoginRequest
import com.permutados.admin.domain.repository.AuthRepository
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.withContext

class AuthRepositoryImpl(
    private val api: ApiProvider,
    private val store: SessionStore,
    private val allowLocalCleartext: Boolean,
    private val io: CoroutineDispatcher,
    private val nowEpochSec: () -> Long = { System.currentTimeMillis() / 1000 },
) : AuthRepository {
    override suspend fun login(baseUrl: String, username: String, password: String): Outcome<Session> {
        val url = when (val v = BaseUrlValidator.normalize(baseUrl, allowLocalCleartext)) {
            is Outcome.Failure -> return v
            is Outcome.Success -> v.value
        }
        if (username.isBlank() || password.isEmpty()) {
            return Outcome.Failure(ApiError.InvalidInput("Ingresá usuario y contraseña."))
        }
        return withContext(io) {
            when (val r = safeCall { api.auth(url).login(LoginRequest(username.trim(), password)) }) {
                is Outcome.Failure -> r
                is Outcome.Success -> {
                    val s = Session(
                        baseUrl = url,
                        username = username.trim(),
                        role = r.value.role,
                        accessToken = r.value.accessToken,
                        refreshToken = r.value.refreshToken,
                        accessExpiresAtEpochSec = nowEpochSec() + r.value.expiresIn,
                    )
                    store.save(s)
                    Outcome.Success(s)
                }
            }
        }
    }

    /** Revokes the refresh-token family on the backend, then always forgets the local session. */
    override suspend fun logout(): Outcome<Unit> = withContext(io) {
        val s = store.current() ?: return@withContext Outcome.Success(Unit)
        val r = safeCall { api.admin(s.baseUrl).logout() }
        store.clear()
        when (r) {
            is Outcome.Success -> Outcome.Success(Unit)
            is Outcome.Failure -> if (r.error == ApiError.Unauthorized) Outcome.Success(Unit) else r
        }
    }

    override suspend fun lastBaseUrl(): String? = store.lastBaseUrl()
}

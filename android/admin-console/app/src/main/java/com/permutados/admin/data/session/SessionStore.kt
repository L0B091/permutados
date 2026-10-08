package com.permutados.admin.data.session

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

interface SessionStore {
    /** null = logged out. Only meaningful once [ready] is true. */
    val session: StateFlow<Session?>
    val ready: StateFlow<Boolean>

    /** Synchronous read for OkHttp interceptors (served from memory). */
    fun current(): Session? = session.value

    suspend fun save(session: Session)
    suspend fun clear()

    /** Last backend URL used (not secret), to prefill the login screen. */
    suspend fun lastBaseUrl(): String?
}

/** Non-persistent store used by unit tests and previews. */
class InMemorySessionStore(initial: Session? = null) : SessionStore {
    private val state = MutableStateFlow(initial)
    private var url: String? = initial?.baseUrl
    override val session: StateFlow<Session?> = state.asStateFlow()
    override val ready: StateFlow<Boolean> = MutableStateFlow(true)
    override suspend fun save(session: Session) {
        url = session.baseUrl
        state.value = session
    }

    override suspend fun clear() {
        state.value = null
    }

    override suspend fun lastBaseUrl(): String? = url
}

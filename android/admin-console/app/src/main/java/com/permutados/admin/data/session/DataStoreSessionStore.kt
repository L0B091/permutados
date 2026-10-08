package com.permutados.admin.data.session

import android.util.Base64
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import kotlinx.serialization.json.Json

/**
 * Session persisted in Preferences DataStore, encrypted with [TokenCipher] (Android Keystore in production).
 * The plaintext tokens only live in memory. If the blob cannot be decrypted (key invalidated, app data restored
 * on another device) the session is discarded and the admin must log in again.
 */
class DataStoreSessionStore(
    private val dataStore: DataStore<Preferences>,
    private val cipher: TokenCipher,
    private val json: Json,
    scope: CoroutineScope,
    private val encoder: (ByteArray) -> String = { Base64.encodeToString(it, Base64.NO_WRAP) },
    private val decoder: (String) -> ByteArray = { Base64.decode(it, Base64.NO_WRAP) },
) : SessionStore {
    private val state = MutableStateFlow<Session?>(null)
    private val readyState = MutableStateFlow(false)
    override val session: StateFlow<Session?> = state.asStateFlow()
    override val ready: StateFlow<Boolean> = readyState.asStateFlow()

    init {
        scope.launch {
            state.value = load()
            readyState.value = true
        }
    }

    private suspend fun load(): Session? {
        val blob = dataStore.data.first()[KEY_SESSION] ?: return null
        return try {
            json.decodeFromString(Session.serializer(), cipher.decrypt(decoder(blob)).decodeToString())
        } catch (e: Exception) {
            dataStore.edit { it.remove(KEY_SESSION) }
            null
        }
    }

    override suspend fun save(session: Session) {
        val blob = encoder(cipher.encrypt(json.encodeToString(Session.serializer(), session).encodeToByteArray()))
        dataStore.edit {
            it[KEY_SESSION] = blob
            it[KEY_BASE_URL] = session.baseUrl
        }
        state.value = session
    }

    override suspend fun clear() {
        state.value = null
        dataStore.edit { it.remove(KEY_SESSION) }
    }

    override suspend fun lastBaseUrl(): String? = dataStore.data.first()[KEY_BASE_URL]

    companion object {
        val KEY_SESSION = stringPreferencesKey("session_encrypted")
        val KEY_BASE_URL = stringPreferencesKey("last_base_url")
    }
}

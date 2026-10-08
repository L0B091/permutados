package com.permutados.admin

import android.content.Context
import androidx.datastore.preferences.preferencesDataStore
import com.permutados.admin.data.remote.ApiJson
import com.permutados.admin.data.remote.ApiProvider
import com.permutados.admin.data.repository.AuthRepositoryImpl
import com.permutados.admin.data.repository.RemoteAdminRepository
import com.permutados.admin.data.session.DataStoreSessionStore
import com.permutados.admin.data.session.KeystoreTokenCipher
import com.permutados.admin.data.session.SessionStore
import com.permutados.admin.domain.repository.AuthRepository
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

private val Context.sessionDataStore by preferencesDataStore(name = "admin_session")

/** Manual dependency injection (small app, no DI framework needed). */
class AppContainer(context: Context) {
    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    val allowLocalCleartext: Boolean = BuildConfig.ALLOW_LOCAL_CLEARTEXT
    val defaultBaseUrl: String = BuildConfig.API_BASE_URL

    val sessionStore: SessionStore =
        DataStoreSessionStore(context.applicationContext.sessionDataStore, KeystoreTokenCipher(), ApiJson, appScope)
    private val apiProvider = ApiProvider(sessionStore, allowLocalCleartext)

    val authRepository: AuthRepository =
        AuthRepositoryImpl(apiProvider, sessionStore, allowLocalCleartext, Dispatchers.IO)
    val adminRepository = RemoteAdminRepository(apiProvider, sessionStore, Dispatchers.IO)
}

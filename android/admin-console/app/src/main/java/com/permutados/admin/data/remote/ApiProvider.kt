package com.permutados.admin.data.remote

import com.permutados.admin.data.session.SessionStore
import kotlinx.serialization.json.Json
import okhttp3.ConnectionSpec
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

val ApiJson: Json = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    coerceInputValues = true
    encodeDefaults = true
}

/**
 * Builds Retrofit clients per backend base URL (the URL is chosen on the login screen).
 * TLS: modern TLS only; cleartext is only enabled for debug builds and the network security config still limits
 * it to 10.0.2.2/localhost. No HTTP logging interceptor is installed, so tokens and bodies never reach logcat.
 */
class ApiProvider(
    private val store: SessionStore,
    allowLocalCleartext: Boolean,
    baseClient: OkHttpClient = defaultClient(allowLocalCleartext),
    private val json: Json = ApiJson,
) {
    private val authClient = baseClient
    private val adminClient = baseClient.newBuilder()
        .addInterceptor(AuthInterceptor(store))
        .authenticator(TokenAuthenticator(store, ::auth))
        .build()
    private val authApis = ConcurrentHashMap<String, AuthApi>()
    private val adminApis = ConcurrentHashMap<String, AdminApi>()

    fun auth(baseUrl: String): AuthApi = authApis.getOrPut(baseUrl) { retrofit(baseUrl, authClient).create(AuthApi::class.java) }

    fun admin(baseUrl: String): AdminApi =
        adminApis.getOrPut(baseUrl) { retrofit(baseUrl, adminClient).create(AdminApi::class.java) }

    private fun retrofit(baseUrl: String, client: OkHttpClient): Retrofit = Retrofit.Builder()
        .baseUrl(baseUrl)
        .client(client)
        .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
        .build()

    companion object {
        fun defaultClient(allowLocalCleartext: Boolean): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .followSslRedirects(false) // never follow an https -> http redirect
            .connectionSpecs(
                if (allowLocalCleartext) listOf(ConnectionSpec.MODERN_TLS, ConnectionSpec.CLEARTEXT)
                else listOf(ConnectionSpec.MODERN_TLS),
            )
            .addInterceptor { chain ->
                chain.proceed(chain.request().newBuilder().header("User-Agent", "PermutadosAdmin-Android/1.0").build())
            }
            .build()
    }
}

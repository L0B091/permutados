package com.permutados.admin.data.remote

import com.permutados.admin.domain.model.LoginRequest
import com.permutados.admin.domain.model.RefreshRequest
import com.permutados.admin.domain.model.TokenResponse
import retrofit2.Call
import retrofit2.http.Body
import retrofit2.http.POST

/** Unauthenticated endpoints. Served by a client without the token authenticator (no refresh loops). */
interface AuthApi {
    @POST("admin/auth/login")
    suspend fun login(@Body body: LoginRequest): TokenResponse

    /** Blocking on purpose: called from OkHttp's [okhttp3.Authenticator] thread. */
    @POST("admin/auth/refresh")
    fun refresh(@Body body: RefreshRequest): Call<TokenResponse>
}

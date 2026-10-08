package com.permutados.admin.data.session

import com.permutados.admin.core.Role
import kotlinx.serialization.Serializable

/** Authenticated admin session. Persisted only encrypted (see [DataStoreSessionStore]); never logged. */
@Serializable
data class Session(
    val baseUrl: String,
    val username: String,
    val role: String,
    val accessToken: String,
    val refreshToken: String,
    val accessExpiresAtEpochSec: Long,
) {
    val roleEnum: Role get() = Role.parse(role)

    override fun toString(): String = "Session(user=$username, role=$role, baseUrl=$baseUrl, tokens=<redacted>)"
}

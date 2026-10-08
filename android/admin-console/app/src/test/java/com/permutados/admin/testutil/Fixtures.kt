package com.permutados.admin.testutil

import com.permutados.admin.data.session.Session

fun session(baseUrl: String, access: String = "access-1", refresh: String = "refresh-1", role: String = "ADMIN") =
    Session(baseUrl, "ana", role, access, refresh, accessExpiresAtEpochSec = 0)

const val TOKEN_JSON_TEMPLATE = """{"access_token":"%s","refresh_token":"%s","token_type":"bearer","expires_in":900,"role":"%s"}"""

fun tokenJson(access: String, refresh: String, role: String = "ADMIN") = TOKEN_JSON_TEMPLATE.format(access, refresh, role)

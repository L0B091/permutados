package com.permutados.admin.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RoleTest {
    @Test fun ordering() {
        assertTrue(Role.ADMIN.atLeast(Role.MODERATOR))
        assertTrue(Role.SUPERADMIN.atLeast(Role.ADMIN))
        assertFalse(Role.VIEWER.atLeast(Role.MODERATOR))
        assertFalse(Role.MODERATOR.atLeast(Role.ADMIN))
    }

    @Test fun unknownRoleFallsBackToLeastPrivilege() {
        assertEquals(Role.VIEWER, Role.parse("ROOT"))
        assertEquals(Role.VIEWER, Role.parse(null))
        assertEquals(Role.MODERATOR, Role.parse("moderator"))
    }
}

class RedactionTest {
    @Test fun secretsNeverAppearInToString() {
        val login = com.permutados.admin.domain.model.LoginRequest("ana", "s3cret-pass")
        val refresh = com.permutados.admin.domain.model.RefreshRequest("refresh-abc")
        val tokens = com.permutados.admin.domain.model.TokenResponse("acc-xyz", "ref-xyz", "bearer", 900, "ADMIN")
        listOf(login.toString() to "s3cret-pass", refresh.toString() to "refresh-abc", tokens.toString() to "acc-xyz", tokens.toString() to "ref-xyz")
            .forEach { (text, secret) -> assertFalse(text, text.contains(secret)) }
    }
}

package com.permutados.admin.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class BaseUrlValidatorTest {
    private fun ok(raw: String, local: Boolean = false) =
        (BaseUrlValidator.normalize(raw, local) as Outcome.Success).value

    private fun rejected(raw: String, local: Boolean = false) =
        BaseUrlValidator.normalize(raw, local) is Outcome.Failure

    @Test fun httpsIsAcceptedAndGetsTrailingSlash() {
        assertEquals("https://api.permutados.ar/", ok("https://api.permutados.ar"))
        assertEquals("https://api.permutados.ar/v1/", ok("  https://api.permutados.ar/v1  "))
    }

    @Test fun cleartextIsRejectedInRelease() {
        assertTrue(rejected("http://api.permutados.ar"))
        assertTrue(rejected("http://10.0.2.2:8000"))
    }

    @Test fun cleartextOnlyForTheDevMachineInDebug() {
        assertEquals("http://10.0.2.2:8000/", ok("http://10.0.2.2:8000", local = true))
        assertEquals("http://localhost:8000/", ok("http://localhost:8000", local = true))
        assertTrue(rejected("http://192.168.0.10:8000", local = true))
        assertTrue(rejected("http://evil.example", local = true))
    }

    @Test fun credentialsQueryAndGarbageAreRejected() {
        assertTrue(rejected(""))
        assertTrue(rejected("not a url"))
        assertTrue(rejected("ftp://api.permutados.ar"))
        assertTrue(rejected("https://user:pass@api.permutados.ar"))
        assertTrue(rejected("https://api.permutados.ar/?token=x"))
    }
}

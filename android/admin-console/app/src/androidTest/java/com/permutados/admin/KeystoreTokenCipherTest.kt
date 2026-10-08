package com.permutados.admin

import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.permutados.admin.data.remote.ApiJson
import com.permutados.admin.data.session.DataStoreSessionStore
import com.permutados.admin.data.session.KeystoreTokenCipher
import com.permutados.admin.data.session.Session
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.security.KeyStore

/**
 * Instrumented (device/emulator) tests for the real Android Keystore path. NOT run in the build box: the only
 * emulator there belongs to another project. Run with `./gradlew connectedDebugAndroidTest` on your own device.
 */
@RunWith(AndroidJUnit4::class)
class KeystoreTokenCipherTest {
    private val alias = "permutados_test_${System.nanoTime()}"

    @Test fun roundTripWithRandomIv() {
        val cipher = KeystoreTokenCipher(alias)
        val plain = "refresh-token-value".encodeToByteArray()
        val a = cipher.encrypt(plain)
        val b = cipher.encrypt(plain)
        assertFalse(a.contentEquals(b)) // fresh IV each time
        assertArrayEquals(plain, cipher.decrypt(a))
        assertArrayEquals(plain, cipher.decrypt(b))
        KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.deleteEntry(alias)
    }

    @Test fun tamperedBlobIsRejected() {
        val cipher = KeystoreTokenCipher(alias)
        val blob = cipher.encrypt("x".encodeToByteArray())
        blob[blob.size - 1] = (blob.last().toInt() xor 1).toByte()
        val failed = runCatching { cipher.decrypt(blob) }.isFailure
        assertEquals(true, failed)
        KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.deleteEntry(alias)
    }

    @Test fun sessionStoreWithKeystorePersistsEncrypted() = runBlocking {
        val ctx = InstrumentationRegistry.getInstrumentation().targetContext
        val file = File(ctx.filesDir, "test_session_${System.nanoTime()}.preferences_pb")
        val job = Job()
        val scope = CoroutineScope(Dispatchers.IO + job)
        val store = DataStoreSessionStore(
            PreferenceDataStoreFactory.create(scope = scope, produceFile = { file }),
            KeystoreTokenCipher(alias),
            ApiJson,
            scope,
        )
        store.ready.first { it }
        store.save(Session("https://api.permutados.example/", "ana", "ADMIN", "ACCESS-XYZ", "REFRESH-XYZ", 0))
        job.cancelAndJoin()
        val raw = file.readBytes().decodeToString()
        assertFalse(raw.contains("ACCESS-XYZ"))
        assertNotEquals(0, raw.length)
        file.delete()
        KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.deleteEntry(alias)
    }
}

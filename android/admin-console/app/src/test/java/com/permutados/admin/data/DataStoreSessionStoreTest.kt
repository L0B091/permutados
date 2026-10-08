package com.permutados.admin.data

import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.edit
import com.permutados.admin.data.remote.ApiJson
import com.permutados.admin.data.session.DataStoreSessionStore
import com.permutados.admin.data.session.TokenCipher
import com.permutados.admin.testutil.session
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.Base64

/** Persistence logic of the encrypted session store, on the JVM with a stand-in cipher. The real Android
 * Keystore cipher is covered by the instrumented test KeystoreTokenCipherTest (needs a device). */
class DataStoreSessionStoreTest {
    @get:Rule val tmp = TemporaryFolder()

    /** Reversible stand-in so the test can prove that what reaches disk is the cipher output, not plaintext. */
    private class XorCipher : TokenCipher {
        var failDecrypt = false
        override fun encrypt(plain: ByteArray) = byteArrayOf(7) + plain.map { (it.toInt() xor 0x5A).toByte() }
        override fun decrypt(blob: ByteArray): ByteArray {
            check(!failDecrypt) { "key invalidated" }
            require(blob[0] == 7.toByte())
            return blob.drop(1).map { (it.toInt() xor 0x5A).toByte() }.toByteArray()
        }
    }

    private fun open(file: File, cipher: TokenCipher): Triple<DataStoreSessionStore, Job, androidx.datastore.core.DataStore<androidx.datastore.preferences.core.Preferences>> {
        val job = Job()
        val scope = CoroutineScope(Dispatchers.IO + job)
        val ds = PreferenceDataStoreFactory.create(scope = scope, produceFile = { file })
        val store = DataStoreSessionStore(
            ds, cipher, ApiJson, scope,
            encoder = { Base64.getEncoder().encodeToString(it) },
            decoder = { Base64.getDecoder().decode(it) },
        )
        return Triple(store, job, ds)
    }

    private fun awaitReady(store: DataStoreSessionStore) = runBlocking { store.ready.first { it } }

    @Test fun sessionSurvivesRestartAndIsNotStoredInPlaintext() = runBlocking {
        val file = File(tmp.root, "admin_session.preferences_pb")
        val cipher = XorCipher()
        val (store, job, _) = open(file, cipher)
        awaitReady(store)
        assertNull(store.current())

        store.save(session("https://api.permutados.example/", access = "ACCESS-SECRET-123", refresh = "REFRESH-SECRET-456"))
        job.cancelAndJoin()

        val raw = file.readBytes().decodeToString()
        assertFalse(raw.contains("ACCESS-SECRET-123"))
        assertFalse(raw.contains("REFRESH-SECRET-456"))

        val (reopened, job2, _) = open(file, cipher)
        awaitReady(reopened)
        assertEquals("ACCESS-SECRET-123", reopened.current()!!.accessToken)
        assertEquals("https://api.permutados.example/", reopened.lastBaseUrl())
        job2.cancelAndJoin()
    }

    @Test fun undecryptableSessionIsDiscarded() = runBlocking {
        val file = File(tmp.root, "s2.preferences_pb")
        val cipher = XorCipher()
        val (store, job, _) = open(file, cipher)
        awaitReady(store)
        store.save(session("https://api.permutados.example/"))
        job.cancelAndJoin()

        cipher.failDecrypt = true // e.g. Keystore key invalidated / data restored on another device
        val (reopened, job2, ds) = open(file, cipher)
        awaitReady(reopened)
        assertNull(reopened.current())
        assertTrue(ds.data.first()[DataStoreSessionStore.KEY_SESSION] == null)
        job2.cancelAndJoin()
    }

    @Test fun clearRemovesTokensButKeepsLastUrl() = runBlocking {
        val file = File(tmp.root, "s3.preferences_pb")
        val (store, job, ds) = open(file, XorCipher())
        awaitReady(store)
        store.save(session("https://api.permutados.example/"))
        store.clear()
        assertNull(store.current())
        assertNull(ds.data.first()[DataStoreSessionStore.KEY_SESSION])
        assertEquals("https://api.permutados.example/", store.lastBaseUrl())
        ds.edit { } // datastore still usable
        job.cancelAndJoin()
    }
}

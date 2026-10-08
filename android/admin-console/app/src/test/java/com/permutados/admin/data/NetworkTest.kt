package com.permutados.admin.data

import com.permutados.admin.core.ApiError
import com.permutados.admin.core.Outcome
import com.permutados.admin.data.remote.ApiProvider
import com.permutados.admin.data.repository.AuthRepositoryImpl
import com.permutados.admin.data.repository.RemoteAdminRepository
import com.permutados.admin.data.session.InMemorySessionStore
import com.permutados.admin.domain.model.ItemEditRequest
import com.permutados.admin.testutil.session
import com.permutados.admin.testutil.tokenJson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.net.InetAddress
import java.util.concurrent.atomic.AtomicInteger

/** Real HTTP against MockWebServer: request shapes, bearer handling, refresh rotation, error mapping. */
class NetworkTest {
    private lateinit var server: MockWebServer
    private lateinit var baseUrl: String

    @Before fun setUp() {
        server = MockWebServer()
        server.start(InetAddress.getByName("127.0.0.1"), 0)
        baseUrl = server.url("/").newBuilder().host("127.0.0.1").build().toString()
    }

    @After fun tearDown() = server.shutdown()

    private fun json(body: String, code: Int = 200) =
        MockResponse().setResponseCode(code).setHeader("Content-Type", "application/json").setBody(body)

    // Debug-like client so the mock server can be plain HTTP on localhost.
    private fun provider(store: InMemorySessionStore) = ApiProvider(store, allowLocalCleartext = true)

    @Test fun loginStoresSessionAndSendsCredentialsOnlyInBody() = runBlocking {
        val store = InMemorySessionStore()
        val repo = AuthRepositoryImpl(provider(store), store, allowLocalCleartext = true, io = Dispatchers.IO, nowEpochSec = { 1000 })
        server.enqueue(json(tokenJson("acc", "ref", "MODERATOR")))

        val r = repo.login(baseUrl.removeSuffix("/"), " ana ", "s3cret")

        assertTrue(r is Outcome.Success)
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/admin/auth/login", req.path)
        assertNull(req.getHeader("Authorization"))
        val body = Json.parseToJsonElement(req.body.readUtf8()).jsonObject
        assertEquals(JsonPrimitive("ana"), body["username"])
        val s = store.current()!!
        assertEquals("MODERATOR", s.role)
        assertEquals("acc", s.accessToken)
        assertEquals(1900, s.accessExpiresAtEpochSec)
        assertFalse("tokens must not appear in toString/logs", s.toString().contains("acc"))
    }

    @Test fun loginRejectsInsecureUrlWithoutAnyNetworkCall() = runBlocking {
        val store = InMemorySessionStore()
        val repo = AuthRepositoryImpl(provider(store), store, allowLocalCleartext = false, io = Dispatchers.IO)
        val r = repo.login("http://api.permutados.example", "ana", "x")
        assertTrue((r as Outcome.Failure).error is ApiError.InvalidInput)
        assertEquals(0, server.requestCount)
        assertNull(store.current())
    }

    @Test fun badCredentialsMapToUnauthorized() = runBlocking {
        val store = InMemorySessionStore()
        val repo = AuthRepositoryImpl(provider(store), store, allowLocalCleartext = true, io = Dispatchers.IO)
        server.enqueue(json("""{"error":"INVALID_CREDENTIALS","message":"invalid credentials"}""", 401))
        val r = repo.login(baseUrl, "ana", "bad")
        assertEquals(ApiError.Unauthorized, (r as Outcome.Failure).error)
        assertNull(store.current())
        // The login endpoint is never retried through the refresh authenticator.
        assertEquals(1, server.requestCount)
    }

    @Test fun bearerIsAddedAndQueryParamsAreSent() = runBlocking {
        val store = InMemorySessionStore(session(baseUrl))
        val repo = RemoteAdminRepository(provider(store), store, Dispatchers.IO)
        server.enqueue(json("""[{"id":7,"whatsapp":"***4455","status":"SUSPENDED","warnings_count":1,"evasion_strikes":2,"created_at":"2026-10-01T12:00:00+00:00","unknown":"ignored"}]"""))

        val r = repo.users("+5491122334455", "SUSPENDED") as Outcome.Success

        assertEquals(7L, r.value.single().id)
        assertEquals(2, r.value.single().evasionStrikes)
        val req = server.takeRequest()
        assertEquals("Bearer access-1", req.getHeader("Authorization"))
        assertEquals("/admin/users", req.requestUrl!!.encodedPath)
        assertEquals("+5491122334455", req.requestUrl!!.queryParameter("q"))
        assertEquals("SUSPENDED", req.requestUrl!!.queryParameter("status"))
    }

    @Test fun editItemIsPatchWithOnlyChangedFields() = runBlocking {
        val store = InMemorySessionStore(session(baseUrl))
        val repo = RemoteAdminRepository(provider(store), store, Dispatchers.IO)
        server.enqueue(json("""{"item_id":"ITEM-ABCDE","owner_id":1,"status":"PUBLISHED","title":"Bici rodado 29"}"""))

        repo.edit("ITEM-ABCDE", ItemEditRequest(title = "Bici rodado 29", reason = "texto ofensivo"))

        val req = server.takeRequest()
        assertEquals("PATCH", req.method)
        assertEquals("/admin/items/ITEM-ABCDE", req.path)
        val body = Json.parseToJsonElement(req.body.readUtf8()).jsonObject
        assertEquals(setOf("title", "reason"), body.keys)
    }

    @Test fun backendErrorsAreMapped() = runBlocking {
        val store = InMemorySessionStore(session(baseUrl, role = "VIEWER"))
        val repo = RemoteAdminRepository(provider(store), store, Dispatchers.IO)
        server.enqueue(json("""{"error":"insufficient role"}""", 403))
        server.enqueue(json("""{"error":"CONTACT_DATA_IN_TEXT","message":"the new text contains contact data"}""", 422))

        assertEquals(ApiError.Forbidden, (repo.suspendUser(1, "spam") as Outcome.Failure).error)
        val e = (repo.edit("ITEM-ABCDE", ItemEditRequest(title = "llamame 1122334455", reason = "x x x")) as Outcome.Failure).error
        assertEquals("CONTACT_DATA_IN_TEXT", e.code)
    }

    @Test fun noSessionMeansNoRequest() = runBlocking {
        val store = InMemorySessionStore()
        val repo = RemoteAdminRepository(provider(store), store, Dispatchers.IO)
        assertEquals(ApiError.Unauthorized, (repo.dashboard() as Outcome.Failure).error)
        assertEquals(0, server.requestCount)
    }

    @Test fun expiredAccessTokenIsRefreshedOnceAndRequestRetried() = runBlocking {
        val store = InMemorySessionStore(session(baseUrl, access = "old", refresh = "r1"))
        val repo = RemoteAdminRepository(provider(store), store, Dispatchers.IO)
        server.enqueue(json("""{"error":"token expired"}""", 401))
        server.enqueue(json(tokenJson("new", "r2")))
        server.enqueue(json("""{"mode":"ONLINE"}"""))

        val r = repo.mode()

        assertEquals("ONLINE", (r as Outcome.Success).value)
        val first = server.takeRequest()
        val refresh = server.takeRequest()
        val retry = server.takeRequest()
        assertEquals("Bearer old", first.getHeader("Authorization"))
        assertEquals("/admin/auth/refresh", refresh.path)
        assertNull(refresh.getHeader("Authorization"))
        assertTrue(refresh.body.readUtf8().contains("\"refresh_token\":\"r1\""))
        assertEquals("Bearer new", retry.getHeader("Authorization"))
        assertEquals("r2", store.current()!!.refreshToken) // rotated
    }

    @Test fun rejectedRefreshClearsTheSession() = runBlocking {
        val store = InMemorySessionStore(session(baseUrl, access = "old", refresh = "revoked"))
        val repo = RemoteAdminRepository(provider(store), store, Dispatchers.IO)
        server.enqueue(json("""{"error":"token expired"}""", 401))
        server.enqueue(json("""{"error":"REFRESH_REUSED"}""", 401))

        val r = repo.dashboard()

        assertEquals(ApiError.Unauthorized, (r as Outcome.Failure).error)
        assertNull("UI must go back to login", store.current())
        assertEquals(2, server.requestCount)
    }

    @Test fun concurrent401sTriggerASingleRefresh() = runBlocking {
        val store = InMemorySessionStore(session(baseUrl, access = "old", refresh = "r1"))
        val repo = RemoteAdminRepository(provider(store), store, Dispatchers.IO)
        val refreshes = AtomicInteger()
        server.dispatcher = object : Dispatcher() {
            override fun dispatch(request: RecordedRequest): MockResponse = when {
                request.path == "/admin/auth/refresh" -> {
                    refreshes.incrementAndGet()
                    Thread.sleep(100)
                    json(tokenJson("new", "r2"))
                }
                request.getHeader("Authorization") == "Bearer new" -> json("""{"mode":"PAUSED"}""")
                else -> json("""{"error":"expired"}""", 401)
            }
        }
        val results = (1..5).map { async(Dispatchers.IO) { repo.mode() } }.awaitAll()
        assertTrue(results.all { it is Outcome.Success && it.value == "PAUSED" })
        assertEquals("refresh-token rotation must not be raced", 1, refreshes.get())
    }

    @Test fun tokenIsNeverSentToAnotherOrigin() = runBlocking {
        val other = MockWebServer().apply { start(InetAddress.getByName("127.0.0.1"), 0) }
        try {
            // The session belongs to another host; a client pointed at this server must not leak the bearer.
            val store = InMemorySessionStore(session("https://api.permutados.example/"))
            val api = provider(store).admin(other.url("/").newBuilder().host("127.0.0.1").build().toString())
            other.enqueue(json("""{"mode":"ONLINE"}"""))
            api.systemMode()
            assertNull(other.takeRequest().getHeader("Authorization"))
        } finally {
            other.shutdown()
        }
    }

    @Test fun logoutRevokesAndAlwaysClearsLocally() = runBlocking {
        val store = InMemorySessionStore(session(baseUrl))
        val repo = AuthRepositoryImpl(provider(store), store, allowLocalCleartext = true, io = Dispatchers.IO)
        server.enqueue(MockResponse().setResponseCode(204))
        assertTrue(repo.logout() is Outcome.Success)
        assertEquals("/admin/auth/logout", server.takeRequest().path)
        assertNull(store.current())

        val store2 = InMemorySessionStore(session(baseUrl))
        val repo2 = AuthRepositoryImpl(provider(store2), store2, allowLocalCleartext = true, io = Dispatchers.IO)
        server.enqueue(MockResponse().setResponseCode(500))
        repo2.logout()
        assertNull("local session is dropped even if the server fails", store2.current())
    }

    @Test fun variableShapeReportsAreKeptAsJson() = runBlocking {
        val store = InMemorySessionStore(session(baseUrl))
        val repo = RemoteAdminRepository(provider(store), store, Dispatchers.IO)
        server.enqueue(json("""{"period_days":7,"users":{"total":3,"new":1},"daily":{"offers":[["2026-10-01",2]]}}"""))
        val m = (repo.metrics(7) as Outcome.Success).value
        assertEquals("7", server.takeRequest().requestUrl!!.queryParameter("days"))
        assertEquals(JsonPrimitive(3), m["users"]!!.jsonObject["total"])
    }
}

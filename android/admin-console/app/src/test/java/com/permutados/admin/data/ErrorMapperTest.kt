package com.permutados.admin.data

import com.permutados.admin.core.ApiError
import com.permutados.admin.data.remote.ErrorMapper
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

class ErrorMapperTest {
    @Test fun backendDomainErrorKeepsCodeAndMessage() {
        val e = ErrorMapper.fromHttp(422, """{"error":"CONTACT_DATA_IN_TEXT","message":"the new text contains contact data","details":{"kinds":["phone"]}}""")
        assertEquals(ApiError.Validation("CONTACT_DATA_IN_TEXT", "the new text contains contact data"), e)
        assertTrue(e.userMessage.contains("CONTACT_DATA_IN_TEXT"))
    }

    @Test fun fastapiValidationDetail() {
        val e = ErrorMapper.fromHttp(422, """{"detail":[{"loc":["body","reason"],"msg":"Field required","type":"missing"}]}""")
        assertEquals(ApiError.Validation(null, "Field required"), e)
    }

    @Test fun statusMapping() {
        assertEquals(ApiError.Unauthorized, ErrorMapper.fromHttp(401, """{"error":"invalid token"}"""))
        assertEquals(ApiError.Forbidden, ErrorMapper.fromHttp(403, """{"error":"insufficient role"}"""))
        assertEquals(ApiError.RateLimited, ErrorMapper.fromHttp(429, null))
        assertEquals(ApiError.Server(503), ErrorMapper.fromHttp(503, "<html>bad gateway</html>"))
        assertEquals(ApiError.Conflict("ITEM_CLOSED", "closed"), ErrorMapper.fromHttp(409, """{"error":"ITEM_CLOSED","message":"closed"}"""))
        assertEquals(ApiError.NotFound(null, null), ErrorMapper.fromHttp(404, "not json"))
    }

    @Test fun transportErrors() {
        assertEquals(ApiError.Network, ErrorMapper.fromThrowable(IOException("reset")))
        assertEquals(ApiError.Parsing, ErrorMapper.fromThrowable(kotlinx.serialization.SerializationException("x")))
    }
}

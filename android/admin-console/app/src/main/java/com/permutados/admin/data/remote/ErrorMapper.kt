package com.permutados.admin.data.remote

import com.permutados.admin.core.ApiError
import com.permutados.admin.core.Outcome
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import retrofit2.HttpException
import java.io.IOException

/** Maps HTTP/transport failures to [ApiError]. Backend errors look like {"error": CODE, "message": ...};
 * FastAPI request-validation errors look like {"detail": [{"msg": ...}]}. */
object ErrorMapper {
    private val lenient = Json { ignoreUnknownKeys = true; isLenient = true }

    fun fromHttp(status: Int, body: String?): ApiError {
        val (code, message) = parseBody(body)
        return when (status) {
            401 -> ApiError.Unauthorized
            403 -> ApiError.Forbidden
            404 -> ApiError.NotFound(code, message)
            409 -> ApiError.Conflict(code, message)
            400, 422 -> ApiError.Validation(code, message)
            429 -> ApiError.RateLimited
            in 500..599 -> ApiError.Server(status)
            else -> ApiError.Http(status, code, message)
        }
    }

    internal fun parseBody(body: String?): Pair<String?, String?> {
        if (body.isNullOrBlank()) return null to null
        val obj = try {
            lenient.parseToJsonElement(body) as? JsonObject
        } catch (e: SerializationException) {
            null
        } ?: return null to null
        val code = (obj["error"] as? JsonPrimitive)?.contentOrNull
        val message = (obj["message"] as? JsonPrimitive)?.contentOrNull
            ?: (obj["detail"] as? JsonPrimitive)?.contentOrNull
            ?: (obj["detail"] as? JsonArray)?.firstOrNull()?.let {
                (it.jsonObject["msg"] as? JsonPrimitive)?.contentOrNull
            }
        return code to message
    }

    fun fromThrowable(t: Throwable): ApiError = when (t) {
        is HttpException -> fromHttp(t.code(), t.response()?.errorBody()?.string())
        is SerializationException -> ApiError.Parsing
        is IllegalArgumentException -> ApiError.Parsing
        is IOException -> ApiError.Network
        else -> ApiError.Http(-1, t.javaClass.simpleName, null)
    }
}

suspend fun <T> safeCall(block: suspend () -> T): Outcome<T> = try {
    Outcome.Success(block())
} catch (e: CancellationException) {
    throw e
} catch (e: Exception) {
    Outcome.Failure(ErrorMapper.fromThrowable(e))
}

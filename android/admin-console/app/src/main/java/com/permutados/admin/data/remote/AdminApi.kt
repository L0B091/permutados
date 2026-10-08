package com.permutados.admin.data.remote

import com.permutados.admin.domain.model.AuditEntry
import com.permutados.admin.domain.model.BanDto
import com.permutados.admin.domain.model.ChannelPost
import com.permutados.admin.domain.model.ChannelPostRequest
import com.permutados.admin.domain.model.ChannelRemovals
import com.permutados.admin.domain.model.ChannelSuggestions
import com.permutados.admin.domain.model.ConversationContext
import com.permutados.admin.domain.model.ConversationDetail
import com.permutados.admin.domain.model.ConversationSummary
import com.permutados.admin.domain.model.Dashboard
import com.permutados.admin.domain.model.FailedRequest
import com.permutados.admin.domain.model.FeatureRequest
import com.permutados.admin.domain.model.ItemDetail
import com.permutados.admin.domain.model.ItemEditRequest
import com.permutados.admin.domain.model.ItemSummary
import com.permutados.admin.domain.model.Me
import com.permutados.admin.domain.model.ModeRequest
import com.permutados.admin.domain.model.NotesRequest
import com.permutados.admin.domain.model.OfferSummary
import com.permutados.admin.domain.model.Operation
import com.permutados.admin.domain.model.OperationDetail
import com.permutados.admin.domain.model.Payment
import com.permutados.admin.domain.model.PublishedRequest
import com.permutados.admin.domain.model.QueueRequest
import com.permutados.admin.domain.model.ReasonRequest
import com.permutados.admin.domain.model.RemovalRequest
import com.permutados.admin.domain.model.ReportDto
import com.permutados.admin.domain.model.ResolveReportRequest
import com.permutados.admin.domain.model.ResolveReportResult
import com.permutados.admin.domain.model.SecurityEventDto
import com.permutados.admin.domain.model.SystemModeDto
import com.permutados.admin.domain.model.UserDetail
import com.permutados.admin.domain.model.UserSummary
import com.permutados.admin.domain.model.WarningDto
import com.permutados.admin.domain.model.WorkersStatus
import kotlinx.serialization.json.JsonObject
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.PATCH
import retrofit2.http.POST
import retrofit2.http.PUT
import retrofit2.http.Path
import retrofit2.http.Query

/**
 * Backend admin API (JWT bearer, added by [AuthInterceptor]). Every path here is verified against the FastAPI
 * routes by the backend test `tests/integration/test_android_contract.py`, so the app cannot call an endpoint
 * that does not exist.
 */
interface AdminApi {
    // auth
    @GET("admin/auth/me") suspend fun me(): Me
    @POST("admin/auth/logout") suspend fun logout(): Response<Unit>

    // dashboard / metrics
    @GET("admin/dashboard") suspend fun dashboard(): Dashboard
    @GET("admin/metrics") suspend fun metrics(@Query("days") days: Int): JsonObject

    // users
    @GET("admin/users")
    suspend fun users(
        @Query("q") q: String?,
        @Query("status") status: String?,
        @Query("limit") limit: Int,
        @Query("offset") offset: Int,
    ): List<UserSummary>

    @GET("admin/users/{id}") suspend fun user(@Path("id") id: Long): UserDetail
    @POST("admin/users/{id}/suspend") suspend fun suspend(@Path("id") id: Long, @Body body: ReasonRequest): UserSummary
    @POST("admin/users/{id}/ban") suspend fun ban(@Path("id") id: Long, @Body body: NotesRequest): UserSummary
    @POST("admin/users/{id}/reinstate") suspend fun reinstate(@Path("id") id: Long, @Body body: NotesRequest): UserSummary

    // publications
    @GET("admin/items")
    suspend fun items(
        @Query("status") status: String?,
        @Query("q") q: String?,
        @Query("limit") limit: Int,
        @Query("offset") offset: Int,
    ): List<ItemSummary>

    @GET("admin/items/{code}") suspend fun item(@Path("code") code: String): ItemDetail
    @PATCH("admin/items/{code}") suspend fun editItem(@Path("code") code: String, @Body body: ItemEditRequest): ItemDetail
    @POST("admin/items/{code}/remove") suspend fun removeItem(@Path("code") code: String, @Body body: ReasonRequest): ItemSummary
    @POST("admin/items/{code}/feature") suspend fun featureItem(@Path("code") code: String, @Body body: FeatureRequest): ItemSummary
    @GET("admin/items/{code}/score") suspend fun itemScore(@Path("code") code: String): JsonObject

    // offers / operations / payments
    @GET("admin/offers")
    suspend fun offers(@Query("status") status: String?, @Query("limit") limit: Int, @Query("offset") offset: Int): List<OfferSummary>

    @GET("admin/transactions")
    suspend fun transactions(@Query("status") status: String?, @Query("limit") limit: Int, @Query("offset") offset: Int): List<Operation>

    @GET("admin/transactions/{code}") suspend fun transaction(@Path("code") code: String): OperationDetail

    @GET("admin/payments")
    suspend fun payments(@Query("status") status: String?, @Query("limit") limit: Int, @Query("offset") offset: Int): List<Payment>

    // conversations (opening one is audited by the backend)
    @GET("admin/conversations")
    suspend fun conversations(@Query("q") q: String?, @Query("limit") limit: Int, @Query("offset") offset: Int): List<ConversationSummary>

    @GET("admin/conversations/{userId}")
    suspend fun conversation(@Path("userId") userId: Long, @Query("limit") limit: Int): ConversationDetail

    @GET("admin/conversations/{userId}/context")
    suspend fun conversationContext(@Path("userId") userId: Long): ConversationContext

    // security
    @GET("admin/security/bans") suspend fun bans(@Query("status") status: String?): List<BanDto>
    @GET("admin/security/warnings") suspend fun warnings(@Query("limit") limit: Int): List<WarningDto>
    @GET("admin/security/events") suspend fun securityEvents(@Query("limit") limit: Int): List<SecurityEventDto>
    @GET("admin/reports") suspend fun reports(@Query("status") status: String?): List<ReportDto>
    @POST("admin/reports/{id}/resolve")
    suspend fun resolveReport(@Path("id") id: Long, @Body body: ResolveReportRequest): ResolveReportResult

    @GET("admin/audit")
    suspend fun audit(@Query("action") action: String?, @Query("limit") limit: Int): List<AuditEntry>

    // channel (manual administration)
    @GET("admin/channel/overview") suspend fun channelOverview(): JsonObject
    @GET("admin/channel/suggestions") suspend fun channelSuggestions(@Query("limit") limit: Int): ChannelSuggestions
    @GET("admin/channel/posts")
    suspend fun channelPosts(@Query("status") status: String?, @Query("limit") limit: Int): List<ChannelPost>

    @GET("admin/channel/removals") suspend fun channelRemovals(): ChannelRemovals
    @POST("admin/channel/queue") suspend fun channelQueue(@Body body: QueueRequest): List<ChannelPost>
    @POST("admin/channel/posts") suspend fun channelRegister(@Body body: ChannelPostRequest): ChannelPost
    @POST("admin/channel/posts/{id}/published")
    suspend fun channelPublished(@Path("id") id: Long, @Body body: PublishedRequest): ChannelPost

    @POST("admin/channel/posts/{id}/failed")
    suspend fun channelFailed(@Path("id") id: Long, @Body body: FailedRequest): ChannelPost

    @POST("admin/channel/posts/{id}/retry") suspend fun channelRetry(@Path("id") id: Long): ChannelPost
    @POST("admin/channel/posts/{id}/remove")
    suspend fun channelRequestRemoval(@Path("id") id: Long, @Body body: RemovalRequest): ChannelPost

    @POST("admin/channel/posts/{id}/removed") suspend fun channelRemoved(@Path("id") id: Long): ChannelPost
    @POST("admin/channel/reconcile") suspend fun channelReconcile(): JsonObject
    @POST("admin/channel/refresh-scores") suspend fun channelRefreshScores(): JsonObject

    // configuration / system
    @GET("admin/config") suspend fun config(): JsonObject
    @PUT("admin/config") suspend fun updateConfig(@Body body: JsonObject): JsonObject
    @GET("admin/system/mode") suspend fun systemMode(): SystemModeDto
    @PUT("admin/system/mode") suspend fun setSystemMode(@Body body: ModeRequest): SystemModeDto
    @GET("admin/system/health") suspend fun systemHealth(): JsonObject
    @GET("admin/workers") suspend fun workers(): WorkersStatus
}

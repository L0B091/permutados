package com.permutados.admin.data.repository

import com.permutados.admin.core.ApiError
import com.permutados.admin.core.Outcome
import com.permutados.admin.core.map
import com.permutados.admin.data.remote.AdminApi
import com.permutados.admin.data.remote.ApiProvider
import com.permutados.admin.data.remote.safeCall
import com.permutados.admin.data.session.SessionStore
import com.permutados.admin.domain.model.ChannelPostRequest
import com.permutados.admin.domain.model.FailedRequest
import com.permutados.admin.domain.model.FeatureRequest
import com.permutados.admin.domain.model.ItemEditRequest
import com.permutados.admin.domain.model.ModeRequest
import com.permutados.admin.domain.model.NotesRequest
import com.permutados.admin.domain.model.PublishedRequest
import com.permutados.admin.domain.model.QueueRequest
import com.permutados.admin.domain.model.ReasonRequest
import com.permutados.admin.domain.model.RemovalRequest
import com.permutados.admin.domain.model.ResolveReportRequest
import com.permutados.admin.domain.repository.ChannelRepository
import com.permutados.admin.domain.repository.ConversationsRepository
import com.permutados.admin.domain.repository.DashboardRepository
import com.permutados.admin.domain.repository.OperationsRepository
import com.permutados.admin.domain.repository.PublicationsRepository
import com.permutados.admin.domain.repository.SecurityRepository
import com.permutados.admin.domain.repository.SystemRepository
import com.permutados.admin.domain.repository.UsersRepository
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

/** Retrofit-backed implementation of every admin domain repository. All data comes from the backend; the app
 * keeps no local copy of PII and computes nothing it shows as fact. */
class RemoteAdminRepository(
    private val provider: ApiProvider,
    private val store: SessionStore,
    private val io: CoroutineDispatcher,
) : DashboardRepository, UsersRepository, PublicationsRepository, OperationsRepository, ConversationsRepository,
    SecurityRepository, ChannelRepository, SystemRepository {

    private suspend fun <T> call(block: suspend AdminApi.() -> T): Outcome<T> {
        val session = store.current() ?: return Outcome.Failure(ApiError.Unauthorized)
        return withContext(io) { safeCall { provider.admin(session.baseUrl).block() } }
    }

    private fun String?.clean(): String? = this?.trim()?.takeIf { it.isNotEmpty() }

    // dashboard
    override suspend fun dashboard() = call { dashboard() }
    override suspend fun metrics(days: Int) = call { metrics(days) }

    // users
    override suspend fun users(query: String?, status: String?) = call { users(query.clean(), status, PAGE, 0) }
    override suspend fun user(id: Long) = call { user(id) }
    override suspend fun suspendUser(id: Long, reason: String) = call { suspend(id, ReasonRequest(reason.trim())) }
    override suspend fun ban(id: Long, notes: String?) = call { ban(id, NotesRequest(notes.clean())) }
    override suspend fun reinstate(id: Long, notes: String?, resetStrikes: Boolean) =
        call { reinstate(id, NotesRequest(notes.clean(), resetStrikes)) }

    // publications
    override suspend fun items(status: String?, query: String?) = call { items(status, query.clean(), PAGE, 0) }
    override suspend fun item(code: String) = call { item(code.trim().uppercase()) }
    override suspend fun edit(code: String, request: ItemEditRequest) = call { editItem(code, request) }
    override suspend fun remove(code: String, reason: String) = call { removeItem(code, ReasonRequest(reason.trim())) }
    override suspend fun feature(code: String, featured: Boolean) = call { featureItem(code, FeatureRequest(featured)) }
    override suspend fun score(code: String) = call { itemScore(code) }

    // operations
    override suspend fun offers(status: String?) = call { offers(status, PAGE, 0) }
    override suspend fun operations(status: String?) = call { transactions(status, PAGE, 0) }
    override suspend fun operation(code: String) = call { transaction(code.trim().uppercase()) }
    override suspend fun payments(status: String?) = call { payments(status, PAGE, 0) }

    // conversations
    override suspend fun conversations(query: String?) = call { conversations(query.clean(), PAGE, 0) }
    override suspend fun conversation(userId: Long) = call { conversation(userId, PAGE) }
    override suspend fun context(userId: Long) = call { conversationContext(userId) }

    // security
    override suspend fun bans(status: String?) = call { bans(status) }
    override suspend fun warnings() = call { warnings(PAGE) }
    override suspend fun events() = call { securityEvents(PAGE) }
    override suspend fun reports(status: String?) = call { reports(status) }
    override suspend fun resolveReport(id: Long, status: String, notes: String?) =
        call { resolveReport(id, ResolveReportRequest(status, notes.clean())) }.map { }

    override suspend fun audit(action: String?) = call { audit(action.clean(), PAGE) }

    // channel
    override suspend fun overview() = call { channelOverview() }
    override suspend fun suggestions(limit: Int) = call { channelSuggestions(limit) }
    override suspend fun posts(status: String?) = call { channelPosts(status, PAGE) }
    override suspend fun removals() = call { channelRemovals() }
    override suspend fun queue(itemCodes: List<String>) = call { channelQueue(QueueRequest(itemCodes)) }
    override suspend fun register(itemCode: String, externalRef: String?) =
        call { channelRegister(ChannelPostRequest(itemCode.trim().uppercase(), externalRef.clean())) }

    override suspend fun markPublished(postId: Long, externalRef: String?) =
        call { channelPublished(postId, PublishedRequest(externalRef.clean())) }

    override suspend fun markFailed(postId: Long, error: String) = call { channelFailed(postId, FailedRequest(error.trim())) }
    override suspend fun retry(postId: Long) = call { channelRetry(postId) }
    override suspend fun requestRemoval(postId: Long, reason: String) =
        call { channelRequestRemoval(postId, RemovalRequest(reason.trim().ifEmpty { "ADMIN" })) }

    override suspend fun confirmRemoved(postId: Long) = call { channelRemoved(postId) }
    override suspend fun reconcile() = call { channelReconcile() }
    override suspend fun refreshScores() = call { channelRefreshScores() }

    // system
    override suspend fun config() = call { config() }
    override suspend fun updateConfig(changes: Map<String, JsonElement>) = call { updateConfig(JsonObject(changes)) }
    override suspend fun mode() = call { systemMode() }.map { it.mode }
    override suspend fun setMode(mode: String, reason: String) = call { setSystemMode(ModeRequest(mode, reason.trim())) }.map { it.mode }
    override suspend fun health() = call { systemHealth() }
    override suspend fun workers() = call { workers() }

    private companion object {
        const val PAGE = 100
    }
}

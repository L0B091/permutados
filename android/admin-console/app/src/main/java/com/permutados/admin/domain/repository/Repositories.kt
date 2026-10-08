package com.permutados.admin.domain.repository

import com.permutados.admin.core.Outcome
import com.permutados.admin.data.session.Session
import com.permutados.admin.domain.model.AuditEntry
import com.permutados.admin.domain.model.BanDto
import com.permutados.admin.domain.model.ChannelPost
import com.permutados.admin.domain.model.ChannelRemovals
import com.permutados.admin.domain.model.ChannelSuggestions
import com.permutados.admin.domain.model.ConversationContext
import com.permutados.admin.domain.model.ConversationDetail
import com.permutados.admin.domain.model.ConversationSummary
import com.permutados.admin.domain.model.Dashboard
import com.permutados.admin.domain.model.ItemDetail
import com.permutados.admin.domain.model.ItemEditRequest
import com.permutados.admin.domain.model.ItemSummary
import com.permutados.admin.domain.model.OfferSummary
import com.permutados.admin.domain.model.Operation
import com.permutados.admin.domain.model.OperationDetail
import com.permutados.admin.domain.model.Payment
import com.permutados.admin.domain.model.ReportDto
import com.permutados.admin.domain.model.SecurityEventDto
import com.permutados.admin.domain.model.UserDetail
import com.permutados.admin.domain.model.UserSummary
import com.permutados.admin.domain.model.WarningDto
import com.permutados.admin.domain.model.WorkersStatus
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

// Domain contracts. ViewModels depend only on these interfaces; tests replace them with fakes.

interface AuthRepository {
    suspend fun login(baseUrl: String, username: String, password: String): Outcome<Session>
    suspend fun logout(): Outcome<Unit>
    suspend fun lastBaseUrl(): String?
}

interface DashboardRepository {
    suspend fun dashboard(): Outcome<Dashboard>
    suspend fun metrics(days: Int): Outcome<JsonObject>
}

interface UsersRepository {
    suspend fun users(query: String?, status: String?): Outcome<List<UserSummary>>
    suspend fun user(id: Long): Outcome<UserDetail>
    suspend fun suspendUser(id: Long, reason: String): Outcome<UserSummary>
    suspend fun ban(id: Long, notes: String?): Outcome<UserSummary>
    suspend fun reinstate(id: Long, notes: String?, resetStrikes: Boolean): Outcome<UserSummary>
}

interface PublicationsRepository {
    suspend fun items(status: String?, query: String?): Outcome<List<ItemSummary>>
    suspend fun item(code: String): Outcome<ItemDetail>
    suspend fun edit(code: String, request: ItemEditRequest): Outcome<ItemDetail>
    suspend fun remove(code: String, reason: String): Outcome<ItemSummary>
    suspend fun feature(code: String, featured: Boolean): Outcome<ItemSummary>
    suspend fun score(code: String): Outcome<JsonObject>
}

interface OperationsRepository {
    suspend fun offers(status: String?): Outcome<List<OfferSummary>>
    suspend fun operations(status: String?): Outcome<List<Operation>>
    suspend fun operation(code: String): Outcome<OperationDetail>
    suspend fun payments(status: String?): Outcome<List<Payment>>
}

interface ConversationsRepository {
    suspend fun conversations(query: String?): Outcome<List<ConversationSummary>>
    suspend fun conversation(userId: Long): Outcome<ConversationDetail>
    suspend fun context(userId: Long): Outcome<ConversationContext>
}

interface SecurityRepository {
    suspend fun bans(status: String?): Outcome<List<BanDto>>
    suspend fun warnings(): Outcome<List<WarningDto>>
    suspend fun events(): Outcome<List<SecurityEventDto>>
    suspend fun reports(status: String?): Outcome<List<ReportDto>>
    suspend fun resolveReport(id: Long, status: String, notes: String?): Outcome<Unit>
    suspend fun audit(action: String?): Outcome<List<AuditEntry>>
}

interface ChannelRepository {
    suspend fun overview(): Outcome<JsonObject>
    suspend fun suggestions(limit: Int): Outcome<ChannelSuggestions>
    suspend fun posts(status: String?): Outcome<List<ChannelPost>>
    suspend fun removals(): Outcome<ChannelRemovals>
    suspend fun queue(itemCodes: List<String>): Outcome<List<ChannelPost>>
    suspend fun register(itemCode: String, externalRef: String?): Outcome<ChannelPost>
    suspend fun markPublished(postId: Long, externalRef: String?): Outcome<ChannelPost>
    suspend fun markFailed(postId: Long, error: String): Outcome<ChannelPost>
    suspend fun retry(postId: Long): Outcome<ChannelPost>
    suspend fun requestRemoval(postId: Long, reason: String): Outcome<ChannelPost>
    suspend fun confirmRemoved(postId: Long): Outcome<ChannelPost>
    suspend fun reconcile(): Outcome<JsonObject>
    suspend fun refreshScores(): Outcome<JsonObject>
    suspend fun feature(code: String, featured: Boolean): Outcome<ItemSummary>
}

interface SystemRepository {
    suspend fun config(): Outcome<JsonObject>
    suspend fun updateConfig(changes: Map<String, JsonElement>): Outcome<JsonObject>
    suspend fun mode(): Outcome<String>
    suspend fun setMode(mode: String, reason: String): Outcome<String>
    suspend fun health(): Outcome<JsonObject>
    suspend fun workers(): Outcome<WorkersStatus>
}

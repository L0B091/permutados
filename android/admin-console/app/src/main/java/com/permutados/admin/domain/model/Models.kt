@file:Suppress("PropertyName")

package com.permutados.admin.domain.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject

// Wire models of the backend admin API (/admin/...). They are plain Kotlin data classes (kotlinx.serialization
// annotations only, no Android types) and double as domain models. Unknown fields are ignored so the backend can
// add data without breaking the console. Variable-shape reports (metrics, overview, reconciliation, config, health)
// are kept as JsonObject and rendered generically instead of being re-modelled or invented in the app.

// ---- auth -------------------------------------------------------------------------------------------------
// Credentials/tokens are redacted from toString() so they can never end up in a log or crash report.
@Serializable
data class LoginRequest(val username: String, val password: String) {
    override fun toString() = "LoginRequest(username=$username, password=<redacted>)"
}

@Serializable
data class RefreshRequest(@SerialName("refresh_token") val refreshToken: String) {
    override fun toString() = "RefreshRequest(<redacted>)"
}

@Serializable
data class TokenResponse(
    @SerialName("access_token") val accessToken: String,
    @SerialName("refresh_token") val refreshToken: String,
    @SerialName("token_type") val tokenType: String = "bearer",
    @SerialName("expires_in") val expiresIn: Long,
    val role: String,
) {
    override fun toString() = "TokenResponse(role=$role, expiresIn=$expiresIn, tokens=<redacted>)"
}

@Serializable
data class Me(val id: Long, val username: String, val role: String)

// ---- dashboard --------------------------------------------------------------------------------------------
@Serializable
data class Dashboard(
    val users: Long = 0,
    @SerialName("users_suspended") val usersSuspended: Long = 0,
    @SerialName("users_banned") val usersBanned: Long = 0,
    @SerialName("items_published") val itemsPublished: Long = 0,
    @SerialName("items_reserved") val itemsReserved: Long = 0,
    @SerialName("offers_pending") val offersPending: Long = 0,
    @SerialName("offers_accepted_unpaid") val offersAcceptedUnpaid: Long = 0,
    @SerialName("contacts_released") val contactsReleased: Long = 0,
    @SerialName("payments_approved") val paymentsApproved: Long = 0,
    @SerialName("payments_needing_refund") val paymentsNeedingRefund: Long = 0,
    val revenue: String = "0",
    @SerialName("bans_pending_review") val bansPendingReview: Long = 0,
    @SerialName("reports_open") val reportsOpen: Long = 0,
    @SerialName("channel_removal_pending") val channelRemovalPending: Long = 0,
    @SerialName("system_mode") val systemMode: String = "",
)

// ---- users ------------------------------------------------------------------------------------------------
@Serializable
data class UserSummary(
    val id: Long,
    val whatsapp: String? = null, // masked by the backend (last 4 digits)
    val status: String,
    @SerialName("warnings_count") val warningsCount: Int = 0,
    @SerialName("evasion_strikes") val evasionStrikes: Int = 0,
    @SerialName("created_at") val createdAt: String? = null,
)

@Serializable
data class WarningBrief(val id: Long, val reason: String? = null, @SerialName("created_at") val createdAt: String? = null)

@Serializable
data class BanBrief(val id: Long, val status: String, val reason: String? = null)

@Serializable
data class UserDetail(
    val id: Long,
    val whatsapp: String? = null,
    val status: String,
    @SerialName("warnings_count") val warningsCount: Int = 0,
    @SerialName("evasion_strikes") val evasionStrikes: Int = 0,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("display_name") val displayName: String? = null,
    val warnings: List<WarningBrief> = emptyList(),
    val bans: List<BanBrief> = emptyList(),
    val items: List<ItemSummary> = emptyList(),
    val offers: List<OfferSummary> = emptyList(),
    val reports: List<ReportDto> = emptyList(),
)

// ---- publications -----------------------------------------------------------------------------------------
@Serializable
data class ItemSummary(
    @SerialName("item_id") val itemId: String,
    @SerialName("owner_id") val ownerId: Long,
    val status: String,
    val title: String? = null,
    val category: String? = null,
    val price: String? = null,
    val currency: String? = null,
    val featured: Boolean = false,
    val score: Double? = null,
    @SerialName("published_at") val publishedAt: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
)

@Serializable
data class ItemChannelPost(
    val id: Long,
    val status: String,
    @SerialName("posted_at") val postedAt: String? = null,
    @SerialName("removal_reason") val removalReason: String? = null,
)

@Serializable
data class ItemDetail(
    @SerialName("item_id") val itemId: String,
    @SerialName("owner_id") val ownerId: Long,
    val status: String,
    val title: String? = null,
    val category: String? = null,
    val price: String? = null,
    val currency: String? = null,
    val featured: Boolean = false,
    val score: Double? = null,
    @SerialName("published_at") val publishedAt: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
    val description: String? = null,
    val zone: String? = null,
    @SerialName("accepts_swap") val acceptsSwap: Boolean? = null,
    @SerialName("has_damage") val hasDamage: Boolean? = null,
    val works: Boolean? = null,
    @SerialName("channel_status") val channelStatus: String? = null,
    @SerialName("score_breakdown") val scoreBreakdown: JsonElement? = null,
    val images: Int = 0,
    val owner: UserSummary? = null,
    val offers: List<OfferSummary> = emptyList(),
    val reports: List<ReportDto> = emptyList(),
    @SerialName("channel_posts") val channelPosts: List<ItemChannelPost> = emptyList(),
)

@Serializable
data class ItemEditRequest(
    val title: String? = null,
    val category: String? = null,
    val description: String? = null,
    val reason: String,
)

@Serializable
data class FeatureRequest(val featured: Boolean)

// ---- offers / operations / payments -----------------------------------------------------------------------
@Serializable
data class OfferSummary(
    @SerialName("offer_id") val offerId: String,
    @SerialName("item_id") val itemPk: Long? = null,
    @SerialName("item_code") val itemCode: String? = null,
    @SerialName("item_title") val itemTitle: String? = null,
    @SerialName("bidder_id") val bidderId: Long,
    @SerialName("owner_id") val ownerId: Long,
    val type: String,
    val price: String? = null,
    @SerialName("offered_item") val offeredItem: String? = null,
    val status: String,
    @SerialName("close_reason") val closeReason: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("expires_at") val expiresAt: String? = null,
    @SerialName("responded_at") val respondedAt: String? = null,
    @SerialName("closed_at") val closedAt: String? = null,
)

@Serializable
data class Operation(
    @SerialName("transaction_id") val transactionId: String,
    @SerialName("item_code") val itemCode: String? = null,
    @SerialName("item_title") val itemTitle: String? = null,
    @SerialName("offer_id") val offerId: String? = null,
    @SerialName("owner_id") val ownerId: Long,
    @SerialName("bidder_id") val bidderId: Long,
    val status: String,
    val amount: String,
    val currency: String,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("payment_due_at") val paymentDueAt: String? = null,
    @SerialName("paid_at") val paidAt: String? = null,
    @SerialName("released_at") val releasedAt: String? = null,
    @SerialName("closed_at") val closedAt: String? = null,
    @SerialName("close_reason") val closeReason: String? = null,
    @SerialName("contact_released") val contactReleased: Boolean = false,
    @SerialName("keyword_sent") val keywordSent: Boolean = false,
)

@Serializable
data class ReleaseNotification(
    val kind: String,
    val status: String,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("sent_at") val sentAt: String? = null,
)

@Serializable
data class OperationDetail(
    @SerialName("transaction_id") val transactionId: String,
    @SerialName("item_code") val itemCode: String? = null,
    @SerialName("item_title") val itemTitle: String? = null,
    @SerialName("offer_id") val offerId: String? = null,
    @SerialName("owner_id") val ownerId: Long,
    @SerialName("bidder_id") val bidderId: Long,
    val status: String,
    val amount: String,
    val currency: String,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("payment_due_at") val paymentDueAt: String? = null,
    @SerialName("paid_at") val paidAt: String? = null,
    @SerialName("released_at") val releasedAt: String? = null,
    @SerialName("closed_at") val closedAt: String? = null,
    @SerialName("close_reason") val closeReason: String? = null,
    @SerialName("contact_released") val contactReleased: Boolean = false,
    @SerialName("keyword_sent") val keywordSent: Boolean = false,
    val offer: OfferSummary? = null,
    val owner: UserSummary? = null,
    val bidder: UserSummary? = null,
    val payments: List<Payment> = emptyList(),
    @SerialName("release_notifications") val releaseNotifications: List<ReleaseNotification> = emptyList(),
)

@Serializable
data class Payment(
    val id: Long,
    @SerialName("transaction_code") val transactionCode: String? = null,
    val provider: String,
    @SerialName("provider_payment_id") val providerPaymentId: String? = null,
    val status: String,
    @SerialName("provider_status") val providerStatus: String? = null,
    val amount: String,
    val currency: String,
    @SerialName("live_mode") val liveMode: Boolean? = null,
    @SerialName("created_at") val createdAt: String? = null,
    @SerialName("verified_at") val verifiedAt: String? = null,
)

// ---- conversations ----------------------------------------------------------------------------------------
@Serializable
data class ConversationSummary(
    val id: Long,
    val whatsapp: String? = null,
    val status: String,
    @SerialName("evasion_strikes") val evasionStrikes: Int = 0,
    val messages: Int = 0,
    @SerialName("blocked_messages") val blockedMessages: Int = 0,
    @SerialName("last_message_at") val lastMessageAt: String? = null,
    val state: String? = null,
    val intent: String? = null,
)

@Serializable
data class ChatMessage(
    val id: Long,
    val direction: String,
    val type: String,
    val body: String? = null,
    val evidence: String? = null,
    val blocked: Boolean = false,
    @SerialName("created_at") val createdAt: String? = null,
)

@Serializable
data class ConversationDetail(
    @SerialName("user_id") val userId: Long,
    val state: String? = null,
    val messages: List<ChatMessage> = emptyList(),
)

@Serializable
data class ConversationContext(
    @SerialName("user_id") val userId: Long,
    val state: String? = null,
    val intent: String? = null,
    @SerialName("current_item") val currentItem: String? = null,
    @SerialName("selected_item") val selectedItem: String? = null,
    val offer: String? = null,
    val transaction: String? = null,
    @SerialName("last_inbound_at") val lastInboundAt: String? = null,
)

// ---- security ---------------------------------------------------------------------------------------------
@Serializable
data class BanDto(
    val id: Long,
    @SerialName("user_id") val userId: Long,
    val status: String,
    val reason: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
)

@Serializable
data class WarningDto(
    val id: Long,
    @SerialName("user_id") val userId: Long,
    val reason: String? = null,
    @SerialName("message_sent") val messageSent: String? = null,
    @SerialName("created_at") val createdAt: String? = null,
)

@Serializable
data class SecurityEventDto(
    val id: Long,
    @SerialName("user_id") val userId: Long? = null,
    val type: String,
    val severity: String,
    val details: JsonElement? = null,
    @SerialName("created_at") val createdAt: String? = null,
)

@Serializable
data class ReportDto(
    val id: Long,
    @SerialName("reporter_id") val reporterId: Long? = null,
    @SerialName("item_id") val itemPk: Long? = null,
    @SerialName("reported_user_id") val reportedUserId: Long? = null,
    val reason: String,
    val details: String? = null,
    val status: String,
    @SerialName("created_at") val createdAt: String? = null,
)

@Serializable
data class ResolveReportRequest(val status: String, val notes: String? = null)

@Serializable
data class ResolveReportResult(val id: Long, val status: String)

@Serializable
data class AuditEntry(
    val id: Long,
    val action: String,
    @SerialName("actor_type") val actorType: String,
    @SerialName("actor_id") val actorId: Long? = null,
    @SerialName("entity_type") val entityType: String? = null,
    @SerialName("entity_id") val entityId: String? = null,
    val details: JsonElement? = null,
    @SerialName("created_at") val createdAt: String? = null,
)

@Serializable
data class ReasonRequest(val reason: String)

@Serializable
data class NotesRequest(val notes: String? = null, @SerialName("reset_strikes") val resetStrikes: Boolean = false)

// ---- channel ----------------------------------------------------------------------------------------------
@Serializable
data class ChannelPost(
    val id: Long,
    @SerialName("item_id") val itemCode: String,
    @SerialName("item_title") val itemTitle: String? = null,
    @SerialName("item_status") val itemStatus: String? = null,
    @SerialName("channel_status") val channelStatus: String? = null,
    val status: String,
    @SerialName("score_at_post") val scoreAtPost: Double? = null,
    @SerialName("external_ref") val externalRef: String? = null,
    @SerialName("posted_at") val postedAt: String? = null,
    @SerialName("removal_reason") val removalReason: String? = null,
    @SerialName("removal_requested_at") val removalRequestedAt: String? = null,
    @SerialName("last_error") val lastError: String? = null,
    @SerialName("hours_pending") val hoursPending: Double? = null,
    val overdue: Boolean? = null,
)

@Serializable
data class ChannelSuggestion(
    val position: Int,
    @SerialName("item_id") val itemCode: String,
    val title: String? = null,
    val category: String? = null,
    val price: String? = null,
    @SerialName("accepts_swap") val acceptsSwap: Boolean? = null,
    val featured: Boolean = false,
    val score: Double = 0.0,
    val priority: Double = 0.0,
    val exposures: Int = 0,
)

@Serializable
data class ChannelSuggestions(
    val enabled: Boolean = false,
    val quota: JsonObject? = null,
    val items: List<ChannelSuggestion> = emptyList(),
    val reason: String? = null,
    val skipped: JsonObject? = null,
    val candidates: Int? = null,
)

@Serializable
data class ChannelRemovals(
    @SerialName("removal_pending") val removalPending: List<ChannelPost> = emptyList(),
    @SerialName("closed_but_visible_unflagged") val closedButVisibleUnflagged: List<ChannelPost> = emptyList(),
    @SerialName("overdue_hours_threshold") val overdueHoursThreshold: Int? = null,
)

@Serializable
data class QueueRequest(@SerialName("item_ids") val itemIds: List<String>)

@Serializable
data class ChannelPostRequest(
    @SerialName("item_id") val itemCode: String,
    @SerialName("external_ref") val externalRef: String? = null,
    val position: Int? = null,
)

@Serializable
data class PublishedRequest(@SerialName("external_ref") val externalRef: String? = null, val position: Int? = null)

@Serializable
data class FailedRequest(val error: String)

@Serializable
data class RemovalRequest(val reason: String = "ADMIN")

// ---- system -----------------------------------------------------------------------------------------------
@Serializable
data class SystemModeDto(val mode: String)

@Serializable
data class ModeRequest(val mode: String, val reason: String)

@Serializable
data class WorkerDto(
    @SerialName("worker_id") val workerId: String,
    val hostname: String? = null,
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("last_seen_at") val lastSeenAt: String? = null,
    val alive: Boolean = false,
    @SerialName("stopped_at") val stoppedAt: String? = null,
)

@Serializable
data class JobDto(
    val job: String,
    val status: String,
    @SerialName("last_started_at") val lastStartedAt: String? = null,
    @SerialName("last_success_at") val lastSuccessAt: String? = null,
    @SerialName("last_error") val lastError: String? = null,
    @SerialName("last_affected") val lastAffected: Int? = null,
    @SerialName("last_duration_ms") val lastDurationMs: Long? = null,
    @SerialName("consecutive_failures") val consecutiveFailures: Int = 0,
)

@Serializable
data class JobRunDto(
    val job: String,
    val status: String,
    @SerialName("started_at") val startedAt: String? = null,
    @SerialName("duration_ms") val durationMs: Long? = null,
    val affected: Int? = null,
    val error: String? = null,
)

@Serializable
data class WorkersStatus(
    val workers: List<WorkerDto> = emptyList(),
    val jobs: List<JobDto> = emptyList(),
    @SerialName("recent_runs") val recentRuns: List<JobRunDto> = emptyList(),
)

object Statuses {
    val USER = listOf("ACTIVE", "SUSPENDED", "BANNED")
    val ITEM = listOf("PUBLISHED", "RESERVED", "COMPLETED", "EXPIRED", "DELETED", "DRAFT")
    val OFFER =
        listOf("PENDING", "ACCEPTED", "PAYMENT_PENDING", "PAID", "CONTACT_RELEASED", "REJECTED", "EXPIRED", "CANCELLED")
    val TRANSACTION = listOf("PAYMENT_PENDING", "PAID", "CONTACT_RELEASED", "CANCELLED", "EXPIRED")
    val PAYMENT = listOf("CREATED", "PENDING", "APPROVED", "REJECTED", "CANCELLED", "REFUNDED", "NEEDS_REFUND")
    val REPORT = listOf("OPEN", "REVIEWING", "RESOLVED", "DISMISSED")
    val BAN = listOf("PENDING_REVIEW", "CONFIRMED", "LIFTED")
    val CHANNEL_POST =
        listOf("PENDING", "SENDING", "PUBLISHED", "FAILED", "RETRY_PENDING", "REMOVAL_PENDING", "REMOVED")
    val SYSTEM_MODE = listOf("ONLINE", "PAUSED", "EMERGENCY")
}

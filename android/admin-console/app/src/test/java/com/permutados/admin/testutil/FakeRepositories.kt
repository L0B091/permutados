package com.permutados.admin.testutil

import com.permutados.admin.core.ApiError
import com.permutados.admin.core.Outcome
import com.permutados.admin.data.session.Session
import com.permutados.admin.domain.model.ChannelPost
import com.permutados.admin.domain.model.ChannelRemovals
import com.permutados.admin.domain.model.ChannelSuggestion
import com.permutados.admin.domain.model.ChannelSuggestions
import com.permutados.admin.domain.model.ItemDetail
import com.permutados.admin.domain.model.ItemEditRequest
import com.permutados.admin.domain.model.ItemSummary
import com.permutados.admin.domain.model.UserDetail
import com.permutados.admin.domain.model.UserSummary
import com.permutados.admin.domain.model.WorkersStatus
import com.permutados.admin.domain.repository.AuthRepository
import com.permutados.admin.domain.repository.ChannelRepository
import com.permutados.admin.domain.repository.PublicationsRepository
import com.permutados.admin.domain.repository.SystemRepository
import com.permutados.admin.domain.repository.UsersRepository
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

class FakeAuthRepository(var result: Outcome<Session>) : AuthRepository {
    val calls = mutableListOf<Triple<String, String, String>>()
    override suspend fun login(baseUrl: String, username: String, password: String): Outcome<Session> {
        calls += Triple(baseUrl, username, password)
        return result
    }
    override suspend fun logout(): Outcome<Unit> = Outcome.Success(Unit)
    override suspend fun lastBaseUrl(): String? = "https://saved.permutados.example/"
}

class FakeUsersRepository : UsersRepository {
    var status = "ACTIVE"
    var actionError: ApiError? = null
    val actions = mutableListOf<String>()
    var loads = 0
    private fun summary() = UserSummary(id = 5, whatsapp = "***4455", status = status)
    override suspend fun users(query: String?, status: String?) = Outcome.Success(listOf(summary()))
    override suspend fun user(id: Long): Outcome<UserDetail> {
        loads++
        return Outcome.Success(UserDetail(id = id, whatsapp = "***4455", status = status))
    }
    private fun act(name: String, newStatus: String): Outcome<UserSummary> {
        actions += name
        actionError?.let { return Outcome.Failure(it) }
        status = newStatus
        return Outcome.Success(summary())
    }
    override suspend fun suspendUser(id: Long, reason: String) = act("suspend:$reason", "SUSPENDED")
    override suspend fun ban(id: Long, notes: String?) = act("ban:$notes", "BANNED")
    override suspend fun reinstate(id: Long, notes: String?, resetStrikes: Boolean) = act("reinstate:$resetStrikes", "ACTIVE")
}

class FakePublicationsRepository : PublicationsRepository {
    var detail = ItemDetail(itemId = "ITEM-ABCDE", ownerId = 1, status = "PUBLISHED", title = "Bici", category = "deportes", description = "Rodado 29")
    val edits = mutableListOf<ItemEditRequest>()
    var editError: ApiError? = null
    override suspend fun items(status: String?, query: String?) =
        Outcome.Success(listOf(ItemSummary(itemId = detail.itemId, ownerId = 1, status = detail.status)))
    override suspend fun item(code: String) = Outcome.Success(detail)
    override suspend fun edit(code: String, request: ItemEditRequest): Outcome<ItemDetail> {
        edits += request
        editError?.let { return Outcome.Failure(it) }
        detail = detail.copy(title = request.title ?: detail.title)
        return Outcome.Success(detail)
    }
    override suspend fun remove(code: String, reason: String): Outcome<ItemSummary> {
        detail = detail.copy(status = "DELETED")
        return Outcome.Success(ItemSummary(itemId = code, ownerId = 1, status = "DELETED"))
    }
    override suspend fun feature(code: String, featured: Boolean): Outcome<ItemSummary> {
        detail = detail.copy(featured = featured)
        return Outcome.Success(ItemSummary(itemId = code, ownerId = 1, status = detail.status, featured = featured))
    }
    override suspend fun score(code: String) = Outcome.Success(JsonObject(mapOf("score" to JsonPrimitive(71))))
}

class FakeChannelRepository : ChannelRepository {
    val queued = mutableListOf<List<String>>()
    val actions = mutableListOf<String>()
    private fun post(id: Long, status: String) = ChannelPost(id = id, itemCode = "ITEM-ABCDE", status = status)
    override suspend fun overview() = Outcome.Success(JsonObject(mapOf("mode" to JsonPrimitive("MANUAL"))))
    override suspend fun suggestions(limit: Int) = Outcome.Success(
        ChannelSuggestions(enabled = true, items = listOf(ChannelSuggestion(1, "ITEM-ABCDE"), ChannelSuggestion(2, "ITEM-FGHJK"))),
    )
    override suspend fun posts(status: String?) = Outcome.Success(listOf(post(1, "PUBLISHED")))
    override suspend fun removals() = Outcome.Success(ChannelRemovals(removalPending = listOf(post(1, "REMOVAL_PENDING"))))
    override suspend fun queue(itemCodes: List<String>): Outcome<List<ChannelPost>> {
        queued += itemCodes
        return Outcome.Success(itemCodes.mapIndexed { i, _ -> post(i.toLong(), "PENDING") })
    }
    override suspend fun register(itemCode: String, externalRef: String?) = rec("register:$itemCode", "PUBLISHED")
    override suspend fun markPublished(postId: Long, externalRef: String?) = rec("published:$postId:$externalRef", "PUBLISHED")
    override suspend fun markFailed(postId: Long, error: String) = rec("failed:$postId", "FAILED")
    override suspend fun retry(postId: Long) = rec("retry:$postId", "RETRY_PENDING")
    override suspend fun requestRemoval(postId: Long, reason: String) = rec("remove:$postId:$reason", "REMOVAL_PENDING")
    override suspend fun confirmRemoved(postId: Long) = rec("removed:$postId", "REMOVED")
    override suspend fun reconcile() = Outcome.Success(JsonObject(mapOf("fixed" to JsonObject(mapOf("closed_items_flagged" to JsonPrimitive(2))))))
    override suspend fun refreshScores() = Outcome.Success(JsonObject(mapOf("changed" to JsonPrimitive(4))))
    override suspend fun feature(code: String, featured: Boolean) = Outcome.Success(ItemSummary(itemId = code, ownerId = 1, status = "PUBLISHED"))
    private fun rec(a: String, status: String): Outcome<ChannelPost> {
        actions += a
        return Outcome.Success(post(1, status))
    }
}

class FakeSystemRepository : SystemRepository {
    var config = JsonObject(
        mapOf(
            "contact_release_price" to JsonPrimitive(3000),
            "contact_release_currency" to JsonPrimitive("ARS"),
            "offer_expiration_hours" to JsonPrimitive(24),
            "channel_enabled" to JsonPrimitive(true),
            "system_mode" to JsonPrimitive("ONLINE"),
        ),
    )
    var mode = "ONLINE"
    val updates = mutableListOf<Map<String, JsonElement>>()
    val modeChanges = mutableListOf<Pair<String, String>>()
    override suspend fun config() = Outcome.Success(config)
    override suspend fun updateConfig(changes: Map<String, JsonElement>): Outcome<JsonObject> {
        updates += changes
        config = JsonObject(config + changes)
        return Outcome.Success(config)
    }
    override suspend fun mode() = Outcome.Success(mode)
    override suspend fun setMode(mode: String, reason: String): Outcome<String> {
        modeChanges += mode to reason
        this.mode = mode
        return Outcome.Success(mode)
    }
    override suspend fun health() = Outcome.Success(JsonObject(mapOf("postgresql" to JsonPrimitive("ok"))))
    override suspend fun workers() = Outcome.Success(WorkersStatus())
}

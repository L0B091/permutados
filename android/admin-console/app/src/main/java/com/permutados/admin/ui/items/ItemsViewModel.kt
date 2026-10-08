package com.permutados.admin.ui.items

import com.permutados.admin.core.ApiError
import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.model.ItemDetail
import com.permutados.admin.domain.model.ItemEditRequest
import com.permutados.admin.domain.model.ItemSummary
import com.permutados.admin.domain.repository.PublicationsRepository
import com.permutados.admin.ui.common.LoadViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.json.JsonObject

class ItemsViewModel(private val repo: PublicationsRepository) : LoadViewModel<List<ItemSummary>>() {
    private val _query = MutableStateFlow("")
    val query: StateFlow<String> = _query.asStateFlow()
    private val _status = MutableStateFlow<String?>(null)
    val status: StateFlow<String?> = _status.asStateFlow()

    init { refresh() }

    fun onQuery(q: String) { _query.value = q }
    fun onStatus(s: String?) { _status.value = s; refresh() }

    override suspend fun fetch(): Outcome<List<ItemSummary>> = repo.items(_status.value, _query.value)
}

class ItemDetailViewModel(private val repo: PublicationsRepository, val code: String) : LoadViewModel<ItemDetail>() {
    private val _score = MutableStateFlow<JsonObject?>(null)
    val score: StateFlow<JsonObject?> = _score.asStateFlow()

    init { refresh() }

    override suspend fun fetch(): Outcome<ItemDetail> = repo.item(code)

    /** Moderation edit of the texts. Only changed fields are sent; the backend rejects contact data. */
    fun edit(current: ItemDetail, title: String, category: String, description: String, reason: String) {
        val req = ItemEditRequest(
            title = title.trim().takeIf { it != (current.title ?: "") },
            category = category.trim().takeIf { it != (current.category ?: "") },
            description = description.trim().takeIf { it != (current.description ?: "") },
            reason = reason.trim(),
        )
        if (req.title == null && req.category == null && req.description == null) {
            showMessage(ApiError.InvalidInput("No hay cambios para guardar.").userMessage)
            return
        }
        action("Publicación editada.") { repo.edit(code, req) }
    }

    fun remove(reason: String) = action("Publicación eliminada.") { repo.remove(code, reason) }
    fun setFeatured(featured: Boolean) =
        action(if (featured) "Marcada como destacada." else "Destacado quitado.") { repo.feature(code, featured) }

    fun loadScore() = action<JsonObject>("Puntaje calculado.", reload = false, onSuccess = { _score.value = it }) {
        repo.score(code)
    }
}

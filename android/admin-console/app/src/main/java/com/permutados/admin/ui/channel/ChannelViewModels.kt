package com.permutados.admin.ui.channel

import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.model.ChannelPost
import com.permutados.admin.domain.model.ChannelRemovals
import com.permutados.admin.domain.model.ChannelSuggestions
import com.permutados.admin.domain.repository.ChannelRepository
import com.permutados.admin.ui.common.LoadViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.serialization.json.JsonObject

/** Overview: quota, posts by status, featured items, channel jobs, last reconciliation; reconcile / refresh. */
class ChannelOverviewViewModel(private val repo: ChannelRepository) : LoadViewModel<JsonObject>() {
    private val _lastResult = MutableStateFlow<JsonObject?>(null)
    val lastResult: StateFlow<JsonObject?> = _lastResult.asStateFlow()

    init { refresh() }
    override suspend fun fetch() = repo.overview()

    fun reconcile() = action<JsonObject>("Reconciliación ejecutada.", onSuccess = { _lastResult.value = it }) { repo.reconcile() }
    fun refreshScores() = action<JsonObject>("Puntajes recalculados.", onSuccess = { _lastResult.value = it }) { repo.refreshScores() }
    fun unfeature(code: String) = action("Destacado quitado.") { repo.feature(code, false) }
}

/** Suggestions to publish by hand (the WhatsApp Channel is administered manually). */
class SuggestionsViewModel(private val repo: ChannelRepository) : LoadViewModel<ChannelSuggestions>() {
    private val _selected = MutableStateFlow<Set<String>>(emptySet())
    val selected: StateFlow<Set<String>> = _selected.asStateFlow()

    init { refresh() }
    override suspend fun fetch() = repo.suggestions(20)

    fun toggle(code: String) = _selected.update { if (code in it) it - code else it + code }

    fun queueSelected() {
        val codes = _selected.value.toList()
        if (codes.isEmpty()) return
        action<List<ChannelPost>>("${codes.size} publicación(es) encolada(s).", onSuccess = { _selected.value = emptySet() }) {
            repo.queue(codes)
        }
    }
}

class ChannelPostsViewModel(private val repo: ChannelRepository) : LoadViewModel<List<ChannelPost>>() {
    private val _status = MutableStateFlow<String?>(null)
    val status: StateFlow<String?> = _status.asStateFlow()

    init { refresh() }
    fun onStatus(s: String?) { _status.value = s; refresh() }
    override suspend fun fetch() = repo.posts(_status.value)

    fun markPublished(id: Long, externalRef: String?) = action("Registrada como publicada.") { repo.markPublished(id, externalRef) }
    fun markFailed(id: Long, error: String) = action("Marcada como fallida.") { repo.markFailed(id, error) }
    fun retry(id: Long) = action("Reintento programado.") { repo.retry(id) }
    fun requestRemoval(id: Long, reason: String) = action("Baja solicitada.") { repo.requestRemoval(id, reason) }
    fun confirmRemoved(id: Long) = action("Eliminación confirmada.") { repo.confirmRemoved(id) }
    fun register(code: String, externalRef: String?) = action("Publicación manual registrada.") { repo.register(code, externalRef) }
}

/** What must be deleted from the Channel now (closed publications still visible). */
class RemovalsViewModel(private val repo: ChannelRepository) : LoadViewModel<ChannelRemovals>() {
    init { refresh() }
    override suspend fun fetch(): Outcome<ChannelRemovals> = repo.removals()
    fun confirmRemoved(id: Long) = action("Eliminación confirmada.") { repo.confirmRemoved(id) }
    fun requestRemoval(id: Long) = action("Baja solicitada.") { repo.requestRemoval(id, "RECONCILIATION") }
}

package com.permutados.admin.ui.conversations

import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.model.ConversationContext
import com.permutados.admin.domain.model.ConversationDetail
import com.permutados.admin.domain.model.ConversationSummary
import com.permutados.admin.domain.repository.ConversationsRepository
import com.permutados.admin.ui.common.LoadViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class ConversationsViewModel(private val repo: ConversationsRepository) : LoadViewModel<List<ConversationSummary>>() {
    private val _query = MutableStateFlow("")
    val query: StateFlow<String> = _query.asStateFlow()

    init { refresh() }

    fun onQuery(q: String) { _query.value = q }
    override suspend fun fetch(): Outcome<List<ConversationSummary>> = repo.conversations(_query.value)
}

data class ConversationView(val context: ConversationContext, val detail: ConversationDetail)

/** Opening a conversation is audited by the backend (ADMIN_VIEWED_CONVERSATION); messages are only fetched here. */
class ConversationDetailViewModel(private val repo: ConversationsRepository, val userId: Long) :
    LoadViewModel<ConversationView>() {
    init { refresh() }

    override suspend fun fetch(): Outcome<ConversationView> {
        val ctx = repo.context(userId)
        if (ctx is Outcome.Failure) return ctx
        return when (val d = repo.conversation(userId)) {
            is Outcome.Failure -> d
            is Outcome.Success -> Outcome.Success(ConversationView((ctx as Outcome.Success).value, d.value))
        }
    }
}

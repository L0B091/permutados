package com.permutados.admin.ui.users

import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.model.UserDetail
import com.permutados.admin.domain.model.UserSummary
import com.permutados.admin.domain.repository.UsersRepository
import com.permutados.admin.ui.common.LoadViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class UsersViewModel(private val repo: UsersRepository) : LoadViewModel<List<UserSummary>>() {
    private val _query = MutableStateFlow("")
    val query: StateFlow<String> = _query.asStateFlow()
    private val _status = MutableStateFlow<String?>(null)
    val status: StateFlow<String?> = _status.asStateFlow()

    init { refresh() }

    fun onQuery(q: String) { _query.value = q }
    fun onStatus(s: String?) { _status.value = s; refresh() }

    /** Exact WhatsApp lookup (the backend searches a blind index; numbers are encrypted at rest). */
    override suspend fun fetch(): Outcome<List<UserSummary>> = repo.users(_query.value, _status.value)
}

class UserDetailViewModel(private val repo: UsersRepository, val userId: Long) : LoadViewModel<UserDetail>() {
    init { refresh() }

    override suspend fun fetch(): Outcome<UserDetail> = repo.user(userId)

    fun suspendUser(reason: String) = action("Usuario suspendido.") { repo.suspendUser(userId, reason) }
    fun ban(notes: String?) = action("Baneo confirmado.") { repo.ban(userId, notes) }
    fun reinstate(notes: String?, resetStrikes: Boolean) =
        action("Usuario rehabilitado.") { repo.reinstate(userId, notes, resetStrikes) }
}

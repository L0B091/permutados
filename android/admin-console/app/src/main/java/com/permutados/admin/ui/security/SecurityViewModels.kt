package com.permutados.admin.ui.security

import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.model.AuditEntry
import com.permutados.admin.domain.model.BanDto
import com.permutados.admin.domain.model.ReportDto
import com.permutados.admin.domain.model.SecurityEventDto
import com.permutados.admin.domain.model.WarningDto
import com.permutados.admin.domain.repository.SecurityRepository
import com.permutados.admin.ui.common.LoadViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class BansViewModel(private val repo: SecurityRepository) : LoadViewModel<List<BanDto>>() {
    private val _status = MutableStateFlow<String?>("PENDING_REVIEW")
    val status: StateFlow<String?> = _status.asStateFlow()
    init { refresh() }
    fun onStatus(s: String?) { _status.value = s; refresh() }
    override suspend fun fetch() = repo.bans(_status.value)
}

class WarningsViewModel(private val repo: SecurityRepository) : LoadViewModel<List<WarningDto>>() {
    init { refresh() }
    override suspend fun fetch() = repo.warnings()
}

class EventsViewModel(private val repo: SecurityRepository) : LoadViewModel<List<SecurityEventDto>>() {
    init { refresh() }
    override suspend fun fetch() = repo.events()
}

class ReportsViewModel(private val repo: SecurityRepository) : LoadViewModel<List<ReportDto>>() {
    private val _status = MutableStateFlow<String?>("OPEN")
    val status: StateFlow<String?> = _status.asStateFlow()
    init { refresh() }
    fun onStatus(s: String?) { _status.value = s; refresh() }
    override suspend fun fetch() = repo.reports(_status.value)
    fun resolve(id: Long, status: String, notes: String?) =
        action("Reporte actualizado a $status.") { repo.resolveReport(id, status, notes) }
}

class AuditViewModel(private val repo: SecurityRepository) : LoadViewModel<List<AuditEntry>>() {
    private val _action = MutableStateFlow("")
    val actionFilter: StateFlow<String> = _action.asStateFlow()
    init { refresh() }
    fun onAction(a: String) { _action.value = a }
    override suspend fun fetch(): Outcome<List<AuditEntry>> = repo.audit(_action.value)
}

package com.permutados.admin.ui.operations

import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.model.OfferSummary
import com.permutados.admin.domain.model.Operation
import com.permutados.admin.domain.model.OperationDetail
import com.permutados.admin.domain.model.Payment
import com.permutados.admin.domain.repository.OperationsRepository
import com.permutados.admin.ui.common.LoadViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Shared "list filtered by status" pattern for offers, operations and payments. */
abstract class StatusListViewModel<T> : LoadViewModel<List<T>>() {
    private val _status = MutableStateFlow<String?>(null)
    val status: StateFlow<String?> = _status.asStateFlow()
    fun onStatus(s: String?) { _status.value = s; refresh() }
    protected val currentStatus: String? get() = _status.value
}

class OffersViewModel(private val repo: OperationsRepository) : StatusListViewModel<OfferSummary>() {
    init { refresh() }
    override suspend fun fetch(): Outcome<List<OfferSummary>> = repo.offers(currentStatus)
}

class OperationsViewModel(private val repo: OperationsRepository) : StatusListViewModel<Operation>() {
    init { refresh() }
    override suspend fun fetch(): Outcome<List<Operation>> = repo.operations(currentStatus)
}

class PaymentsViewModel(private val repo: OperationsRepository) : StatusListViewModel<Payment>() {
    init { refresh() }
    override suspend fun fetch(): Outcome<List<Payment>> = repo.payments(currentStatus)
}

class OperationDetailViewModel(private val repo: OperationsRepository, val code: String) : LoadViewModel<OperationDetail>() {
    init { refresh() }
    override suspend fun fetch(): Outcome<OperationDetail> = repo.operation(code)
}

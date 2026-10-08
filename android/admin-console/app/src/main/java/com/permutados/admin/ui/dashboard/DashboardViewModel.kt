package com.permutados.admin.ui.dashboard

import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.model.Dashboard
import com.permutados.admin.domain.repository.DashboardRepository
import com.permutados.admin.ui.common.LoadViewModel
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.serialization.json.JsonObject

data class DashboardData(val dashboard: Dashboard, val metrics: JsonObject)

class DashboardViewModel(private val repo: DashboardRepository) : LoadViewModel<DashboardData>() {
    private val _days = MutableStateFlow(30)
    val days: StateFlow<Int> = _days.asStateFlow()

    init { refresh() }

    fun setDays(d: Int) {
        _days.value = d
        refresh()
    }

    override suspend fun fetch(): Outcome<DashboardData> = coroutineScope {
        val d = async { repo.dashboard() }
        val m = async { repo.metrics(_days.value) }
        val dash = d.await()
        val met = m.await()
        when {
            dash is Outcome.Failure -> dash
            met is Outcome.Failure -> met
            else -> Outcome.Success(DashboardData((dash as Outcome.Success).value, (met as Outcome.Success).value))
        }
    }
}

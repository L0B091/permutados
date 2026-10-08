package com.permutados.admin.ui.system

import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.model.WorkersStatus
import com.permutados.admin.domain.repository.SystemRepository
import com.permutados.admin.ui.common.LoadViewModel
import kotlinx.coroutines.async
import kotlinx.coroutines.coroutineScope
import kotlinx.serialization.json.JsonObject

data class SystemData(val health: JsonObject, val workers: WorkersStatus)

/** Service status (PostgreSQL, Redis, Dolphin, WhatsApp, Mercado Pago, worker heartbeat) and scheduler jobs. */
class SystemViewModel(private val repo: SystemRepository) : LoadViewModel<SystemData>() {
    init { refresh() }

    override suspend fun fetch(): Outcome<SystemData> = coroutineScope {
        val h = async { repo.health() }
        val w = async { repo.workers() }
        val health = h.await()
        val workers = w.await()
        when {
            health is Outcome.Failure -> health
            workers is Outcome.Failure -> workers
            else -> Outcome.Success(SystemData((health as Outcome.Success).value, (workers as Outcome.Success).value))
        }
    }
}

package com.permutados.admin.ui.system

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.permutados.admin.ui.common.EmptyText
import com.permutados.admin.ui.common.ListCard
import com.permutados.admin.ui.common.SectionTitle
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.StatusBadge
import com.permutados.admin.ui.common.shortTime
import kotlinx.serialization.json.JsonPrimitive

private val SERVICE_LABELS = mapOf(
    "postgresql" to "PostgreSQL",
    "redis" to "Redis",
    "dolphin" to "Dolphin (IA)",
    "whatsapp" to "WhatsApp Cloud API",
    "mercadopago" to "Mercado Pago",
    "worker" to "Worker / scheduler",
    "system_mode" to "Modo",
)

@Composable
fun SystemScreen(vm: SystemViewModel) {
    val state by vm.state.collectAsState()
    StateContent(state, vm::refresh) { data ->
        Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
            OutlinedButton(onClick = vm::refresh) { Text("Actualizar") }
            SectionTitle("Servicios")
            data.health.forEach { (k, v) ->
                val value = (v as? JsonPrimitive)?.content ?: v.toString()
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(vertical = 2.dp)) {
                    Text(SERVICE_LABELS[k] ?: k.replace('_', ' '), Modifier.weight(1f))
                    StatusBadge(if (k == "last_worker_activity") shortTime(value) else value)
                }
            }
            Text(
                "\"configured\" significa que hay credenciales cargadas en el servidor; no implica que el proveedor " +
                    "externo haya respondido. Las credenciales nunca llegan a esta app.",
                style = MaterialTheme.typography.bodySmall,
            )
            SectionTitle("Workers (${data.workers.workers.count { it.alive }} vivos)")
            if (data.workers.workers.isEmpty()) EmptyText("Ningún worker registrado.")
            data.workers.workers.forEach { w ->
                ListCard {
                    Row { Text("${w.workerId} @ ${w.hostname ?: "?"}", Modifier.weight(1f)); StatusBadge(if (w.alive) "ok" else "down") }
                    Text("Inicio ${shortTime(w.startedAt)} · último latido ${shortTime(w.lastSeenAt)}", style = MaterialTheme.typography.bodySmall)
                }
            }
            SectionTitle("Jobs del scheduler")
            data.workers.jobs.forEach { j ->
                ListCard {
                    Row { Text(j.job, Modifier.weight(1f)); StatusBadge(j.status) }
                    Text(
                        "Último OK ${shortTime(j.lastSuccessAt)} · afectados ${j.lastAffected ?: 0} · ${j.lastDurationMs ?: 0} ms · fallos seguidos ${j.consecutiveFailures}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    j.lastError?.let { Text("Error: $it", color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
                }
            }
            SectionTitle("Ejecuciones recientes")
            data.workers.recentRuns.take(30).forEach { r ->
                Text("• ${shortTime(r.startedAt)} ${r.job} ${r.status} (${r.durationMs ?: 0} ms, ${r.affected ?: 0})${r.error?.let { " — $it" } ?: ""}", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

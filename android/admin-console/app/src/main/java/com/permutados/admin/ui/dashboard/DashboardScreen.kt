@file:OptIn(ExperimentalLayoutApi::class)

package com.permutados.admin.ui.dashboard

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.permutados.admin.domain.model.Dashboard
import com.permutados.admin.ui.common.JsonTree
import com.permutados.admin.ui.common.SectionTitle
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.StatusBadge

@Composable
fun DashboardScreen(vm: DashboardViewModel) {
    val state by vm.state.collectAsState()
    val days by vm.days.collectAsState()
    StateContent(state, vm::refresh) { data ->
        Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
            Counters(data.dashboard)
            SectionTitle("Métricas")
            FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                listOf(1, 7, 30, 90).forEach { d ->
                    FilterChip(selected = days == d, onClick = { vm.setDays(d) }, label = { Text("$d días") })
                }
            }
            // Sections come straight from GET /admin/metrics (users, publications, searches, offers, payments,
            // contacts, conversion, channel, errors, bans, performance, daily series).
            data.metrics.forEach { (section, value) ->
                if (section in setOf("generated_at", "period_days", "system_mode")) return@forEach
                Card(Modifier.padding(vertical = 4.dp)) {
                    Column(Modifier.padding(12.dp)) {
                        Text(section.replace('_', ' ').uppercase(), style = MaterialTheme.typography.titleSmall)
                        JsonTree(value)
                    }
                }
            }
        }
    }
}

@Composable
private fun Counters(d: Dashboard) {
    Column {
        Text("Modo del sistema", style = MaterialTheme.typography.labelMedium)
        StatusBadge(d.systemMode)
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Counter("Usuarios", d.users)
            Counter("Suspendidos", d.usersSuspended)
            Counter("Baneados", d.usersBanned)
            Counter("Publicadas", d.itemsPublished)
            Counter("Pausadas (oferta)", d.itemsReserved)
            Counter("Ofertas pendientes", d.offersPending)
            Counter("Aceptadas sin pagar", d.offersAcceptedUnpaid)
            Counter("Contactos liberados", d.contactsReleased)
            Counter("Pagos aprobados", d.paymentsApproved)
            Counter("A reembolsar", d.paymentsNeedingRefund)
            CounterText("Ingresos (ARS)", d.revenue)
            Counter("Revisión de baneo", d.bansPendingReview)
            Counter("Reportes abiertos", d.reportsOpen)
            Counter("Bajas del Canal", d.channelRemovalPending)
        }
    }
}

@Composable
private fun Counter(label: String, value: Long) = CounterText(label, value.toString())

@Composable
private fun CounterText(label: String, value: String) {
    Card(Modifier.width(160.dp)) {
        Column(Modifier.padding(10.dp)) {
            Text(value, style = MaterialTheme.typography.headlineSmall)
            Text(label, style = MaterialTheme.typography.bodySmall)
        }
    }
}

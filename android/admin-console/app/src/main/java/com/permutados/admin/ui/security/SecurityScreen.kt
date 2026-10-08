@file:OptIn(ExperimentalLayoutApi::class)

package com.permutados.admin.ui.security

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ScrollableTabRow
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.permutados.admin.core.Role
import com.permutados.admin.domain.model.ReportDto
import com.permutados.admin.domain.model.Statuses
import com.permutados.admin.ui.common.EmptyText
import com.permutados.admin.ui.common.FilterRow
import com.permutados.admin.ui.common.JsonTree
import com.permutados.admin.ui.common.ListCard
import com.permutados.admin.ui.common.MessageEffect
import com.permutados.admin.ui.common.SearchField
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.StatusBadge
import com.permutados.admin.ui.common.TextInputDialog
import com.permutados.admin.ui.common.appViewModel
import com.permutados.admin.ui.common.shortTime

@Composable
fun SecurityScreen(role: Role, snackbar: SnackbarHostState, onOpenUser: (Long) -> Unit) {
    val tabs = buildList {
        add("Baneos"); add("Advertencias"); add("Reportes"); add("Eventos")
        if (role.atLeast(Role.ADMIN)) add("Auditoría")
    }
    var tab by rememberSaveable { mutableStateOf(0) }
    Column {
        ScrollableTabRow(selectedTabIndex = tab) {
            tabs.forEachIndexed { i, t -> Tab(selected = tab == i, onClick = { tab = i }, text = { Text(t) }) }
        }
        when (tabs[tab]) {
            "Baneos" -> BansTab(appViewModel { BansViewModel(it.adminRepository) }, onOpenUser)
            "Advertencias" -> WarningsTab(appViewModel { WarningsViewModel(it.adminRepository) }, onOpenUser)
            "Reportes" -> ReportsTab(appViewModel { ReportsViewModel(it.adminRepository) }, role, snackbar, onOpenUser)
            "Eventos" -> EventsTab(appViewModel { EventsViewModel(it.adminRepository) }, onOpenUser)
            "Auditoría" -> AuditTab(appViewModel { AuditViewModel(it.adminRepository) })
        }
    }
}

@Composable
private fun BansTab(vm: BansViewModel, onOpenUser: (Long) -> Unit) {
    val state by vm.state.collectAsState()
    val status by vm.status.collectAsState()
    Column {
        FilterRow(Statuses.BAN, status, vm::onStatus)
        Text("2º intento de evasión ⇒ suspensión pendiente de revisión. Abrí el usuario para confirmar o rehabilitar.", style = MaterialTheme.typography.bodySmall)
        StateContent(state, vm::refresh) { bans ->
            if (bans.isEmpty()) EmptyText()
            LazyColumn {
                items(bans, key = { it.id }) { b ->
                    ListCard(onClick = { onOpenUser(b.userId) }) {
                        Row(verticalAlignment = Alignment.CenterVertically) { Text("Baneo #${b.id} · usuario #${b.userId}", Modifier.weight(1f)); StatusBadge(b.status) }
                        Text("${b.reason ?: ""} · ${shortTime(b.createdAt)}", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }
    }
}

@Composable
private fun WarningsTab(vm: WarningsViewModel, onOpenUser: (Long) -> Unit) {
    val state by vm.state.collectAsState()
    StateContent(state, vm::refresh) { list ->
        if (list.isEmpty()) EmptyText()
        LazyColumn {
            items(list, key = { it.id }) { w ->
                ListCard(onClick = { onOpenUser(w.userId) }) {
                    Text("Advertencia #${w.id} · usuario #${w.userId} · ${shortTime(w.createdAt)}")
                    Text(w.reason ?: "", style = MaterialTheme.typography.bodySmall)
                    w.messageSent?.let { Text("Mensaje enviado: $it", style = MaterialTheme.typography.bodySmall) }
                }
            }
        }
    }
}

@Composable
private fun ReportsTab(vm: ReportsViewModel, role: Role, snackbar: SnackbarHostState, onOpenUser: (Long) -> Unit) {
    val state by vm.state.collectAsState()
    val status by vm.status.collectAsState()
    val message by vm.message.collectAsState()
    MessageEffect(message, snackbar, vm::consumeMessage)
    var resolving by remember { mutableStateOf<ReportDto?>(null) }
    var target by remember { mutableStateOf("RESOLVED") }
    Column {
        FilterRow(Statuses.REPORT, status, vm::onStatus)
        StateContent(state, vm::refresh) { list ->
            if (list.isEmpty()) EmptyText()
            LazyColumn {
                items(list, key = { it.id }) { r ->
                    ListCard(onClick = r.reportedUserId?.let { id -> { onOpenUser(id) } }) {
                        Row(verticalAlignment = Alignment.CenterVertically) { Text("Reporte #${r.id} · ${r.reason}", Modifier.weight(1f)); StatusBadge(r.status) }
                        Text(r.details ?: "", style = MaterialTheme.typography.bodySmall)
                        Text("Denunciante #${r.reporterId ?: "—"} · Usuario #${r.reportedUserId ?: "—"} · ${shortTime(r.createdAt)}", style = MaterialTheme.typography.bodySmall)
                        if (role.atLeast(Role.MODERATOR) && r.status in setOf("OPEN", "REVIEWING")) {
                            TextButton(onClick = { resolving = r }) { Text("Resolver") }
                        }
                    }
                }
            }
        }
    }
    resolving?.let { r ->
        TextInputDialog(
            "Resolver reporte #${r.id}",
            "Notas",
            "Guardar",
            minLength = 0,
            extra = {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    listOf("REVIEWING", "RESOLVED", "DISMISSED").forEach { s ->
                        FilterChip(selected = target == s, onClick = { target = s }, label = { Text(s) })
                    }
                }
            },
            onDismiss = { resolving = null },
        ) {
            resolving = null
            vm.resolve(r.id, target, it.ifBlank { null })
        }
    }
}

@Composable
private fun EventsTab(vm: EventsViewModel, onOpenUser: (Long) -> Unit) {
    val state by vm.state.collectAsState()
    StateContent(state, vm::refresh) { list ->
        if (list.isEmpty()) EmptyText()
        LazyColumn {
            items(list, key = { it.id }) { e ->
                ListCard(onClick = e.userId?.let { id -> { onOpenUser(id) } }) {
                    Row(verticalAlignment = Alignment.CenterVertically) { Text("${e.type} · usuario #${e.userId ?: "—"}", Modifier.weight(1f)); StatusBadge(e.severity) }
                    Text(shortTime(e.createdAt), style = MaterialTheme.typography.bodySmall)
                    JsonTree(e.details)
                }
            }
        }
    }
}

@Composable
private fun AuditTab(vm: AuditViewModel) {
    val state by vm.state.collectAsState()
    val action by vm.actionFilter.collectAsState()
    Column {
        SearchField(action, vm::onAction, "Acción (ej. ADMIN_VIEWED_CONVERSATION)", vm::refresh)
        StateContent(state, vm::refresh) { list ->
            if (list.isEmpty()) EmptyText()
            LazyColumn {
                items(list, key = { it.id }) { a ->
                    ListCard {
                        Text("${a.action} · ${shortTime(a.createdAt)}")
                        Text("${a.actorType} #${a.actorId ?: "—"} → ${a.entityType ?: ""} ${a.entityId ?: ""}", style = MaterialTheme.typography.bodySmall)
                        JsonTree(a.details)
                    }
                }
            }
        }
    }
}

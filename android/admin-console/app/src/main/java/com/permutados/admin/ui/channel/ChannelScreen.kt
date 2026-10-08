package com.permutados.admin.ui.channel

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
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
import com.permutados.admin.domain.model.ChannelPost
import com.permutados.admin.domain.model.Statuses
import com.permutados.admin.ui.common.EmptyText
import com.permutados.admin.ui.common.FilterRow
import com.permutados.admin.ui.common.JsonTree
import com.permutados.admin.ui.common.ListCard
import com.permutados.admin.ui.common.MessageEffect
import com.permutados.admin.ui.common.SectionTitle
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.StatusBadge
import com.permutados.admin.ui.common.TextInputDialog
import com.permutados.admin.ui.common.appViewModel
import com.permutados.admin.ui.common.shortTime
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject

/** WhatsApp Channel, administered MANUALLY: the backend suggests, the admin posts by hand and records it here. */
@Composable
fun ChannelScreen(role: Role, snackbar: SnackbarHostState, onOpenItem: (String) -> Unit) {
    val tabs = listOf("Resumen", "Sugerencias", "Publicaciones", "Bajas")
    var tab by rememberSaveable { mutableStateOf(0) }
    Column {
        ScrollableTabRow(selectedTabIndex = tab) {
            tabs.forEachIndexed { i, t -> Tab(selected = tab == i, onClick = { tab = i }, text = { Text(t) }) }
        }
        val canAct = role.atLeast(Role.MODERATOR)
        when (tab) {
            0 -> OverviewTab(appViewModel { ChannelOverviewViewModel(it.adminRepository) }, canAct, snackbar, onOpenItem)
            1 -> SuggestionsTab(appViewModel { SuggestionsViewModel(it.adminRepository) }, canAct, snackbar, onOpenItem)
            2 -> PostsTab(appViewModel { ChannelPostsViewModel(it.adminRepository) }, canAct, snackbar, onOpenItem)
            3 -> RemovalsTab(appViewModel { RemovalsViewModel(it.adminRepository) }, canAct, snackbar, onOpenItem)
        }
    }
}

@Composable
private fun OverviewTab(vm: ChannelOverviewViewModel, canAct: Boolean, snackbar: SnackbarHostState, onOpenItem: (String) -> Unit) {
    val state by vm.state.collectAsState()
    val message by vm.message.collectAsState()
    val busy by vm.busy.collectAsState()
    val last by vm.lastResult.collectAsState()
    MessageEffect(message, snackbar, vm::consumeMessage)
    StateContent(state, vm::refresh) { overview ->
        Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
            if (canAct) {
                Row {
                    Button(onClick = vm::reconcile, enabled = !busy) { Text("Reconciliar ahora") }
                    Spacer(Modifier.padding(4.dp))
                    OutlinedButton(onClick = vm::refreshScores, enabled = !busy) { Text("Recalcular puntajes") }
                }
            }
            last?.let {
                SectionTitle("Resultado de la última acción")
                JsonTree(it)
            }
            SectionTitle("Destacadas")
            val featured = overview["featured"] as? JsonArray
            if (featured.isNullOrEmpty()) EmptyText("Ninguna.")
            featured?.forEach { f ->
                val o = f.jsonObject
                val code = (o["item_id"] as? JsonPrimitive)?.contentOrNull ?: return@forEach
                Row(verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = { onOpenItem(code) }, modifier = Modifier.weight(1f)) {
                        Text("$code · ${(o["title"] as? JsonPrimitive)?.contentOrNull ?: ""} · ${(o["score"] as? JsonPrimitive)?.contentOrNull ?: ""}")
                    }
                    if (canAct) TextButton(onClick = { vm.unfeature(code) }, enabled = !busy) { Text("Quitar") }
                }
            }
            SectionTitle("Estado del Canal")
            JsonTree(kotlinx.serialization.json.JsonObject(overview.filterKeys { it != "featured" }))
        }
    }
}

@Composable
private fun SuggestionsTab(vm: SuggestionsViewModel, canAct: Boolean, snackbar: SnackbarHostState, onOpenItem: (String) -> Unit) {
    val state by vm.state.collectAsState()
    val selected by vm.selected.collectAsState()
    val message by vm.message.collectAsState()
    val busy by vm.busy.collectAsState()
    MessageEffect(message, snackbar, vm::consumeMessage)
    StateContent(state, vm::refresh) { s ->
        Column {
            Column(Modifier.padding(12.dp)) {
                Text(
                    if (s.enabled) "Canal habilitado · candidatos: ${s.candidates ?: 0}" else "Canal deshabilitado en la configuración",
                    style = MaterialTheme.typography.bodyMedium,
                )
                s.reason?.let { Text("Motivo: $it", color = MaterialTheme.colorScheme.error) }
                s.quota?.let { JsonTree(it) }
                if (canAct) {
                    Button(onClick = vm::queueSelected, enabled = selected.isNotEmpty() && !busy) {
                        Text("Encolar seleccionadas (${selected.size})")
                    }
                }
            }
            if (s.items.isEmpty()) EmptyText("Sin sugerencias ahora.")
            LazyColumn {
                items(s.items, key = { it.itemCode }) { it ->
                    ListCard(onClick = { onOpenItem(it.itemCode) }) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            if (canAct) Checkbox(checked = it.itemCode in selected, onCheckedChange = { _ -> vm.toggle(it.itemCode) })
                            Column(Modifier.weight(1f)) {
                                Text("${it.position}. ${it.itemCode}${if (it.featured) " ★" else ""} · ${it.title ?: ""}")
                                Text(
                                    "${it.category ?: ""} · ARS ${it.price ?: "—"} · puntaje ${"%.0f".format(it.score)} · prioridad ${"%.0f".format(it.priority)} · exposiciones ${it.exposures}",
                                    style = MaterialTheme.typography.bodySmall,
                                )
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun PostsTab(vm: ChannelPostsViewModel, canAct: Boolean, snackbar: SnackbarHostState, onOpenItem: (String) -> Unit) {
    val state by vm.state.collectAsState()
    val status by vm.status.collectAsState()
    val message by vm.message.collectAsState()
    val busy by vm.busy.collectAsState()
    MessageEffect(message, snackbar, vm::consumeMessage)
    var dialog by remember { mutableStateOf<Pair<String, ChannelPost?>?>(null) }
    Column {
        FilterRow(Statuses.CHANNEL_POST, status, vm::onStatus)
        if (canAct) {
            OutlinedButton(onClick = { dialog = "register" to null }, modifier = Modifier.padding(horizontal = 12.dp)) {
                Text("Registrar publicación hecha a mano")
            }
        }
        StateContent(state, vm::refresh) { posts ->
            if (posts.isEmpty()) EmptyText()
            LazyColumn {
                items(posts, key = { it.id }) { p ->
                    PostCard(p, onOpenItem) {
                        if (!canAct) return@PostCard
                        Row {
                            when (p.status) {
                                "PENDING", "SENDING", "RETRY_PENDING" -> {
                                    TextButton(onClick = { dialog = "published" to p }, enabled = !busy) { Text("Publicada") }
                                    TextButton(onClick = { dialog = "failed" to p }, enabled = !busy) { Text("Falló") }
                                }
                                "FAILED" -> TextButton(onClick = { vm.retry(p.id) }, enabled = !busy) { Text("Reintentar") }
                                "PUBLISHED" -> TextButton(onClick = { dialog = "remove" to p }, enabled = !busy) { Text("Solicitar baja") }
                                "REMOVAL_PENDING" -> TextButton(onClick = { vm.confirmRemoved(p.id) }, enabled = !busy) { Text("Ya la eliminé") }
                            }
                        }
                    }
                }
            }
        }
    }
    when (val d = dialog) {
        null -> Unit
        else -> when (d.first) {
            "register" -> RegisterDialog(onDismiss = { dialog = null }) { code, ref ->
                dialog = null
                vm.register(code, ref)
            }
            "published" -> TextInputDialog("Registrar como publicada", "Referencia/enlace (opcional)", "Guardar", minLength = 0, onDismiss = { dialog = null }) {
                dialog = null
                vm.markPublished(d.second!!.id, it.ifBlank { null })
            }
            "failed" -> TextInputDialog("Marcar como fallida", "Error", "Guardar", minLength = 2, onDismiss = { dialog = null }) {
                dialog = null
                vm.markFailed(d.second!!.id, it)
            }
            "remove" -> TextInputDialog("Solicitar baja", "Motivo", "Solicitar", minLength = 2, initial = "ADMIN", onDismiss = { dialog = null }) {
                dialog = null
                vm.requestRemoval(d.second!!.id, it)
            }
        }
    }
}

@Composable
private fun RemovalsTab(vm: RemovalsViewModel, canAct: Boolean, snackbar: SnackbarHostState, onOpenItem: (String) -> Unit) {
    val state by vm.state.collectAsState()
    val message by vm.message.collectAsState()
    val busy by vm.busy.collectAsState()
    MessageEffect(message, snackbar, vm::consumeMessage)
    StateContent(state, vm::refresh) { r ->
        LazyColumn {
            item {
                Text(
                    "Publicaciones cerradas que siguen visibles en el Canal. Borralas en WhatsApp y confirmá acá. " +
                        "Vencidas tras ${r.overdueHoursThreshold ?: "?"} h.",
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(12.dp),
                )
            }
            if (r.removalPending.isEmpty() && r.closedButVisibleUnflagged.isEmpty()) item { EmptyText("Nada para eliminar.") }
            items(r.removalPending, key = { "p${it.id}" }) { p ->
                PostCard(p, onOpenItem) {
                    if (canAct) TextButton(onClick = { vm.confirmRemoved(p.id) }, enabled = !busy) { Text("Ya la eliminé") }
                }
            }
            items(r.closedButVisibleUnflagged, key = { "u${it.id}" }) { p ->
                PostCard(p, onOpenItem) {
                    Text("Cerrada pero sin marcar (la reconciliación la marcará).", style = MaterialTheme.typography.bodySmall)
                    if (canAct) TextButton(onClick = { vm.requestRemoval(p.id) }, enabled = !busy) { Text("Marcar para baja") }
                }
            }
        }
    }
}

@Composable
private fun PostCard(p: ChannelPost, onOpenItem: (String) -> Unit, actions: @Composable () -> Unit) {
    ListCard(onClick = { onOpenItem(p.itemCode) }) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Post #${p.id} · ${p.itemCode}", Modifier.weight(1f))
            StatusBadge(p.status)
        }
        Text("${p.itemTitle ?: ""} · publicación ${p.itemStatus ?: ""}", style = MaterialTheme.typography.bodyMedium)
        Text(
            "Publicado ${shortTime(p.postedAt)} · ref ${p.externalRef ?: "—"}" +
                (p.removalReason?.let { " · baja: $it" } ?: "") +
                (p.hoursPending?.let { " · pendiente ${it}h" } ?: "") +
                (if (p.overdue == true) " · VENCIDA" else "") +
                (p.lastError?.let { " · error: $it" } ?: ""),
            style = MaterialTheme.typography.bodySmall,
        )
        actions()
    }
}

@Composable
private fun RegisterDialog(onDismiss: () -> Unit, onSave: (String, String?) -> Unit) {
    var code by remember { mutableStateOf("ITEM-") }
    var ref by remember { mutableStateOf("") }
    val valid = Regex("^ITEM-[23456789ABCDEFGHJKMNPQRSTVWXYZ]{5,8}$").matches(code.trim().uppercase())
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Registrar publicación manual") },
        text = {
            Column(Modifier.fillMaxWidth()) {
                OutlinedTextField(code, { code = it }, label = { Text("Código de la publicación") }, singleLine = true)
                OutlinedTextField(ref, { ref = it }, label = { Text("Referencia/enlace (opcional)") }, singleLine = true)
            }
        },
        confirmButton = { TextButton(onClick = { onSave(code.trim().uppercase(), ref.ifBlank { null }) }, enabled = valid) { Text("Registrar") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancelar") } },
    )
}

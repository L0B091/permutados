package com.permutados.admin.ui.items

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
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.permutados.admin.core.Role
import com.permutados.admin.domain.model.ItemDetail
import com.permutados.admin.domain.model.Statuses
import com.permutados.admin.ui.common.EmptyText
import com.permutados.admin.ui.common.FilterRow
import com.permutados.admin.ui.common.JsonTree
import com.permutados.admin.ui.common.KeyValue
import com.permutados.admin.ui.common.ListCard
import com.permutados.admin.ui.common.MessageEffect
import com.permutados.admin.ui.common.SearchField
import com.permutados.admin.ui.common.SectionTitle
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.StatusBadge
import com.permutados.admin.ui.common.TextInputDialog
import com.permutados.admin.ui.common.shortTime
import com.permutados.admin.ui.offers.OfferRow

private val CLOSED = setOf("COMPLETED", "EXPIRED", "DELETED")

@Composable
fun ItemsScreen(vm: ItemsViewModel, onOpen: (String) -> Unit) {
    val state by vm.state.collectAsState()
    val query by vm.query.collectAsState()
    val status by vm.status.collectAsState()
    Column {
        SearchField(query, vm::onQuery, "Código ITEM-… o título", vm::refresh)
        FilterRow(Statuses.ITEM, status, vm::onStatus)
        StateContent(state, vm::refresh) { items ->
            if (items.isEmpty()) EmptyText()
            LazyColumn {
                items(items, key = { it.itemId }) { i ->
                    ListCard(onClick = { onOpen(i.itemId) }) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("${i.itemId}${if (i.featured) " ★" else ""}", Modifier.weight(1f))
                            StatusBadge(i.status)
                        }
                        Text("${i.title ?: ""} · ${i.category ?: ""} · ${i.currency ?: ""} ${i.price ?: "—"}")
                        Text(
                            "Dueño #${i.ownerId} · Puntaje ${i.score?.let { "%.0f".format(it) } ?: "—"} · Vence ${shortTime(i.expiresAt)}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}

@Composable
fun ItemDetailScreen(
    vm: ItemDetailViewModel,
    role: Role,
    snackbar: SnackbarHostState,
    onOpenUser: (Long) -> Unit,
    onOpenItem: (String) -> Unit,
) {
    val state by vm.state.collectAsState()
    val message by vm.message.collectAsState()
    val busy by vm.busy.collectAsState()
    val score by vm.score.collectAsState()
    MessageEffect(message, snackbar, vm::consumeMessage)
    var dialog by remember { mutableStateOf<String?>(null) }

    StateContent(state, vm::refresh) { item ->
        Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(item.itemId, style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                StatusBadge(item.status)
            }
            Text(item.title ?: "", style = MaterialTheme.typography.titleMedium)
            Text(item.description ?: "", style = MaterialTheme.typography.bodyMedium)
            KeyValue("Categoría", item.category)
            KeyValue("Precio", "${item.currency ?: ""} ${item.price ?: "—"}")
            KeyValue("Zona", item.zone)
            KeyValue("Acepta permuta", item.acceptsSwap?.let { if (it) "Sí" else "No" })
            KeyValue("Funciona", item.works?.let { if (it) "Sí" else "No" })
            KeyValue("Daños", item.hasDamage?.let { if (it) "Sí" else "No" })
            KeyValue("Fotos", item.images.toString())
            KeyValue("Destacada", if (item.featured) "Sí" else "No")
            KeyValue("Estado en Canal", item.channelStatus)
            KeyValue("Publicada", shortTime(item.publishedAt))
            KeyValue("Vence", shortTime(item.expiresAt))
            item.owner?.let { o ->
                OutlinedButton(onClick = { onOpenUser(o.id) }) { Text("Dueño #${o.id} ${o.whatsapp ?: ""} (${o.status})") }
            }

            if (role.atLeast(Role.MODERATOR) && item.status !in CLOSED) {
                Row(Modifier.fillMaxWidth().padding(top = 8.dp)) {
                    Button(onClick = { dialog = "edit" }, enabled = !busy) { Text("Editar textos") }
                    Spacer(Modifier.padding(4.dp))
                    Button(onClick = { dialog = "remove" }, enabled = !busy) { Text("Eliminar") }
                    Spacer(Modifier.padding(4.dp))
                    OutlinedButton(onClick = { vm.setFeatured(!item.featured) }, enabled = !busy) {
                        Text(if (item.featured) "Quitar destacado" else "Destacar")
                    }
                }
            }
            OutlinedButton(onClick = vm::loadScore, enabled = !busy, modifier = Modifier.padding(top = 4.dp)) {
                Text("Calcular puntaje del Canal")
            }
            (score ?: item.scoreBreakdown)?.let {
                SectionTitle("Puntaje")
                JsonTree(it)
            }

            SectionTitle("Ofertas (${item.offers.size})")
            if (item.offers.isEmpty()) EmptyText("Sin ofertas.")
            item.offers.forEach { OfferRow(it, onOpenItem) }

            SectionTitle("Reportes (${item.reports.size})")
            item.reports.forEach { r -> Text("• #${r.id} ${r.reason} — ${r.status} ${r.details ?: ""}") }

            SectionTitle("Publicaciones en el Canal (${item.channelPosts.size})")
            item.channelPosts.forEach { p ->
                Row { Text("• #${p.id} ${shortTime(p.postedAt)} ${p.removalReason ?: ""}", Modifier.weight(1f)); StatusBadge(p.status) }
            }
        }
        when (dialog) {
            "edit" -> EditItemDialog(item, onDismiss = { dialog = null }) { t, c, d, reason ->
                dialog = null
                vm.edit(item, t, c, d, reason)
            }
            "remove" -> TextInputDialog("Eliminar publicación", "Motivo (se audita)", "Eliminar", onDismiss = { dialog = null }) {
                dialog = null
                vm.remove(it)
            }
        }
    }
}

@Composable
private fun EditItemDialog(item: ItemDetail, onDismiss: () -> Unit, onSave: (String, String, String, String) -> Unit) {
    var title by remember { mutableStateOf(item.title ?: "") }
    var category by remember { mutableStateOf(item.category ?: "") }
    var description by remember { mutableStateOf(item.description ?: "") }
    var reason by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Editar publicación") },
        text = {
            Column {
                OutlinedTextField(title, { title = it }, label = { Text("Título") })
                OutlinedTextField(category, { category = it }, label = { Text("Categoría") })
                OutlinedTextField(description, { description = it }, label = { Text("Descripción") }, minLines = 3)
                OutlinedTextField(reason, { reason = it }, label = { Text("Motivo (obligatorio)") })
                Text("El backend rechaza textos con datos de contacto.", style = MaterialTheme.typography.bodySmall)
            }
        },
        confirmButton = {
            TextButton(onClick = { onSave(title, category, description, reason) }, enabled = reason.trim().length >= 3) {
                Text("Guardar")
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancelar") } },
    )
}

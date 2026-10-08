package com.permutados.admin.ui.users

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
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
import com.permutados.admin.domain.model.Statuses
import com.permutados.admin.ui.common.EmptyText
import com.permutados.admin.ui.common.FilterRow
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

@Composable
fun UsersScreen(vm: UsersViewModel, onOpen: (Long) -> Unit) {
    val state by vm.state.collectAsState()
    val query by vm.query.collectAsState()
    val status by vm.status.collectAsState()
    Column {
        SearchField(query, vm::onQuery, "WhatsApp exacto (ej. +5491122334455)", vm::refresh)
        FilterRow(Statuses.USER, status, vm::onStatus)
        StateContent(state, vm::refresh) { users ->
            if (users.isEmpty()) EmptyText()
            LazyColumn {
                items(users, key = { it.id }) { u ->
                    ListCard(onClick = { onOpen(u.id) }) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("#${u.id}  ${u.whatsapp ?: ""}", modifier = Modifier.weight(1f))
                            StatusBadge(u.status)
                        }
                        Text(
                            "Advertencias: ${u.warningsCount} · Intentos de evasión: ${u.evasionStrikes} · Alta: ${shortTime(u.createdAt)}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}

@Composable
fun UserDetailScreen(
    vm: UserDetailViewModel,
    role: Role,
    snackbar: SnackbarHostState,
    onOpenItem: (String) -> Unit,
    onOpenConversation: (Long) -> Unit,
) {
    val state by vm.state.collectAsState()
    val message by vm.message.collectAsState()
    val busy by vm.busy.collectAsState()
    MessageEffect(message, snackbar, vm::consumeMessage)
    var dialog by remember { mutableStateOf<String?>(null) }
    var resetStrikes by remember { mutableStateOf(false) }

    StateContent(state, vm::refresh) { u ->
        Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text("Usuario #${u.id}", style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                StatusBadge(u.status)
            }
            KeyValue("WhatsApp", u.whatsapp)
            KeyValue("Nombre", u.displayName)
            KeyValue("Advertencias", u.warningsCount.toString())
            KeyValue("Intentos evasión", u.evasionStrikes.toString())
            KeyValue("Alta", shortTime(u.createdAt))
            Text(
                "El número completo nunca se muestra: está cifrado en el backend. Esta vista queda auditada.",
                style = MaterialTheme.typography.bodySmall,
            )
            if (role.atLeast(Role.MODERATOR)) {
                Row(Modifier.fillMaxWidth().padding(top = 8.dp)) {
                    if (u.status == "ACTIVE") Button(onClick = { dialog = "suspend" }, enabled = !busy) { Text("Suspender") }
                    if (u.status != "BANNED") {
                        Spacer(Modifier.padding(4.dp))
                        Button(onClick = { dialog = "ban" }, enabled = !busy) { Text("Confirmar baneo") }
                    }
                    if (role.atLeast(Role.ADMIN) && u.status != "ACTIVE") {
                        Spacer(Modifier.padding(4.dp))
                        OutlinedButton(onClick = { dialog = "reinstate" }, enabled = !busy) { Text("Rehabilitar") }
                    }
                }
                OutlinedButton(onClick = { onOpenConversation(u.id) }, modifier = Modifier.padding(top = 4.dp)) {
                    Text("Ver conversación (auditado)")
                }
            }

            SectionTitle("Advertencias (${u.warnings.size})")
            if (u.warnings.isEmpty()) EmptyText("Sin advertencias.")
            u.warnings.forEach { Text("• ${shortTime(it.createdAt)} — ${it.reason ?: ""}") }

            SectionTitle("Baneos")
            if (u.bans.isEmpty()) EmptyText("Sin baneos.")
            u.bans.forEach { b -> Row { Text("• #${b.id} ${b.reason ?: ""}  ", Modifier.weight(1f)); StatusBadge(b.status) } }

            SectionTitle("Publicaciones (${u.items.size})")
            u.items.forEach { i ->
                ListCard(onClick = { onOpenItem(i.itemId) }) {
                    Row { Text("${i.itemId} · ${i.title ?: ""}", Modifier.weight(1f)); StatusBadge(i.status) }
                }
            }

            SectionTitle("Ofertas (${u.offers.size})")
            u.offers.forEach { o -> OfferRow(o, onOpenItem) }

            SectionTitle("Reportes (${u.reports.size})")
            u.reports.forEach { r -> Text("• #${r.id} ${r.reason} — ${r.status} ${r.details ?: ""}") }
        }
    }

    when (dialog) {
        "suspend" -> TextInputDialog("Suspender usuario", "Motivo", "Suspender", onDismiss = { dialog = null }) {
            dialog = null
            vm.suspendUser(it)
        }
        "ban" -> TextInputDialog(
            "Confirmar baneo",
            "Notas de la revisión",
            "Banear",
            minLength = 0,
            onDismiss = { dialog = null },
        ) {
            dialog = null
            vm.ban(it.ifBlank { null })
        }
        "reinstate" -> TextInputDialog(
            "Rehabilitar usuario",
            "Notas",
            "Rehabilitar",
            minLength = 0,
            extra = {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Checkbox(checked = resetStrikes, onCheckedChange = { resetStrikes = it })
                    Text("Reiniciar intentos de evasión")
                }
            },
            onDismiss = { dialog = null },
        ) {
            dialog = null
            vm.reinstate(it.ifBlank { null }, resetStrikes)
        }
    }
}

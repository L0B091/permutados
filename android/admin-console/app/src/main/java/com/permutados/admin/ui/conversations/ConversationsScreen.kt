package com.permutados.admin.ui.conversations

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.permutados.admin.domain.model.ChatMessage
import com.permutados.admin.ui.common.EmptyText
import com.permutados.admin.ui.common.KeyValue
import com.permutados.admin.ui.common.ListCard
import com.permutados.admin.ui.common.SearchField
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.StatusBadge
import com.permutados.admin.ui.common.shortTime

@Composable
fun ConversationsScreen(vm: ConversationsViewModel, onOpen: (Long) -> Unit) {
    val state by vm.state.collectAsState()
    val query by vm.query.collectAsState()
    Column {
        SearchField(query, vm::onQuery, "WhatsApp exacto", vm::refresh)
        Text(
            "Solo metadatos. Abrir una conversación queda registrado en la auditoría.",
            style = MaterialTheme.typography.bodySmall,
            modifier = Modifier.padding(horizontal = 12.dp),
        )
        StateContent(state, vm::refresh) { list ->
            if (list.isEmpty()) EmptyText()
            LazyColumn {
                items(list, key = { it.id }) { c ->
                    ListCard(onClick = { onOpen(c.id) }) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("#${c.id} ${c.whatsapp ?: ""}", Modifier.weight(1f))
                            StatusBadge(c.status)
                        }
                        Text(
                            "${c.messages} mensajes · ${c.blockedMessages} bloqueados · último ${shortTime(c.lastMessageAt)}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                        Text("Estado: ${c.state ?: "—"} · Intención: ${c.intent ?: "—"}", style = MaterialTheme.typography.bodySmall)
                    }
                }
            }
        }
    }
}

@Composable
fun ConversationDetailScreen(vm: ConversationDetailViewModel, onOpenUser: (Long) -> Unit) {
    val state by vm.state.collectAsState()
    StateContent(state, vm::refresh) { view ->
        Column {
            Card(Modifier.fillMaxWidth().padding(12.dp)) {
                Column(Modifier.padding(12.dp)) {
                    Text("Contexto actual", style = MaterialTheme.typography.titleSmall)
                    KeyValue("Estado", view.context.state)
                    KeyValue("Intención", view.context.intent)
                    KeyValue("Publicación", view.context.currentItem ?: view.context.selectedItem)
                    KeyValue("Oferta", view.context.offer)
                    KeyValue("Operación", view.context.transaction)
                    KeyValue("Último entrante", shortTime(view.context.lastInboundAt))
                    OutlinedButton(onClick = { onOpenUser(view.detail.userId) }) { Text("Perfil del usuario #${view.detail.userId}") }
                }
            }
            if (view.detail.messages.isEmpty()) EmptyText("Sin mensajes.")
            // Backend returns newest first; show oldest at the top like a chat.
            LazyColumn(Modifier.padding(horizontal = 12.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                items(view.detail.messages.reversed(), key = { it.id }) { MessageBubble(it) }
            }
        }
    }
}

@Composable
private fun MessageBubble(m: ChatMessage) {
    val inbound = m.direction == "INBOUND"
    Box(Modifier.fillMaxWidth(), contentAlignment = if (inbound) Alignment.CenterStart else Alignment.CenterEnd) {
        Column(
            Modifier.background(
                when {
                    m.blocked -> Color(0x33C62828)
                    inbound -> MaterialTheme.colorScheme.surfaceVariant
                    else -> MaterialTheme.colorScheme.primaryContainer
                },
                RoundedCornerShape(10.dp),
            ).padding(8.dp),
        ) {
            Text(m.body ?: "[${m.type}]")
            if (m.blocked) Text("BLOQUEADO (anti-evasión). Evidencia: ${m.evidence ?: "—"}", color = Color(0xFFC62828), style = MaterialTheme.typography.bodySmall)
            Text("${if (inbound) "Usuario" else "Bot"} · ${shortTime(m.createdAt)}", style = MaterialTheme.typography.labelSmall)
        }
    }
}

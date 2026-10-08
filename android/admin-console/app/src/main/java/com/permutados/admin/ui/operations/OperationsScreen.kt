package com.permutados.admin.ui.operations

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
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
import com.permutados.admin.domain.model.Payment
import com.permutados.admin.domain.model.Statuses
import com.permutados.admin.ui.common.EmptyText
import com.permutados.admin.ui.common.FilterRow
import com.permutados.admin.ui.common.KeyValue
import com.permutados.admin.ui.common.ListCard
import com.permutados.admin.ui.common.SectionTitle
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.StatusBadge
import com.permutados.admin.ui.common.shortTime
import com.permutados.admin.ui.offers.OfferRow

@Composable
fun OperationsScreen(vm: OperationsViewModel, onOpen: (String) -> Unit) {
    val state by vm.state.collectAsState()
    val status by vm.status.collectAsState()
    Column {
        FilterRow(Statuses.TRANSACTION, status, vm::onStatus)
        StateContent(state, vm::refresh) { ops ->
            if (ops.isEmpty()) EmptyText()
            LazyColumn {
                items(ops, key = { it.transactionId }) { t ->
                    ListCard(onClick = { onOpen(t.transactionId) }) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text("${t.transactionId} · ${t.itemCode ?: ""}", Modifier.weight(1f))
                            StatusBadge(t.status)
                        }
                        Text("${t.itemTitle ?: ""} · ${t.currency} ${t.amount}")
                        Text(
                            "Dueño #${t.ownerId} · Ofertante #${t.bidderId} · Pagar antes de ${shortTime(t.paymentDueAt)}" +
                                (if (t.contactReleased) " · Contactos liberados" else ""),
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
            }
        }
    }
}

@Composable
fun OperationDetailScreen(vm: OperationDetailViewModel, onOpenUser: (Long) -> Unit, onOpenItem: (String) -> Unit) {
    val state by vm.state.collectAsState()
    StateContent(state, vm::refresh) { t ->
        Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(t.transactionId, style = MaterialTheme.typography.titleLarge, modifier = Modifier.weight(1f))
                StatusBadge(t.status)
            }
            KeyValue("Publicación", "${t.itemCode ?: ""} ${t.itemTitle ?: ""}")
            KeyValue("Oferta", t.offerId)
            KeyValue("Importe", "${t.currency} ${t.amount}")
            KeyValue("Creada", shortTime(t.createdAt))
            KeyValue("Vence el pago", shortTime(t.paymentDueAt))
            KeyValue("Pagada", shortTime(t.paidAt))
            KeyValue("Liberada", shortTime(t.releasedAt))
            KeyValue("Cerrada", shortTime(t.closedAt))
            KeyValue("Motivo cierre", t.closeReason)
            KeyValue("Contactos liberados", if (t.contactReleased) "Sí" else "No")
            KeyValue("Palabra clave enviada", if (t.keywordSent) "Sí (nunca se muestra)" else "No")
            Row {
                t.owner?.let { OutlinedButton(onClick = { onOpenUser(it.id) }) { Text("Dueño #${it.id}") } }
                t.bidder?.let { OutlinedButton(onClick = { onOpenUser(it.id) }, Modifier.padding(start = 8.dp)) { Text("Ofertante #${it.id}") } }
            }
            t.offer?.let {
                SectionTitle("Oferta")
                OfferRow(it, onOpenItem)
            }
            SectionTitle("Pagos (${t.payments.size})")
            if (t.payments.isEmpty()) EmptyText("Sin pagos.")
            t.payments.forEach { PaymentRow(it, null) }
            SectionTitle("Notificaciones de liberación")
            if (t.releaseNotifications.isEmpty()) EmptyText("Ninguna.")
            t.releaseNotifications.forEach { n ->
                Row { Text("• ${n.kind} · ${shortTime(n.createdAt)} → ${shortTime(n.sentAt)}", Modifier.weight(1f)); StatusBadge(n.status) }
            }
        }
    }
}

@Composable
fun PaymentsScreen(vm: PaymentsViewModel, onOpenOperation: (String) -> Unit) {
    val state by vm.state.collectAsState()
    val status by vm.status.collectAsState()
    Column {
        FilterRow(Statuses.PAYMENT, status, vm::onStatus)
        StateContent(state, vm::refresh) { pays ->
            if (pays.isEmpty()) EmptyText()
            LazyColumn { items(pays, key = { it.id }) { PaymentRow(it, onOpenOperation) } }
        }
    }
}

@Composable
fun PaymentRow(p: Payment, onOpenOperation: ((String) -> Unit)?) {
    ListCard(onClick = if (onOpenOperation != null && p.transactionCode != null) ({ onOpenOperation(p.transactionCode) }) else null) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("Pago #${p.id} · ${p.transactionCode ?: ""}", Modifier.weight(1f))
            StatusBadge(p.status)
        }
        Text("${p.provider} ${p.providerPaymentId ?: ""} · ${p.currency} ${p.amount} · estado proveedor: ${p.providerStatus ?: "—"}")
        Text(
            "Creado ${shortTime(p.createdAt)} · Verificado ${shortTime(p.verifiedAt)}" +
                (p.liveMode?.let { if (it) " · PRODUCCIÓN" else " · SANDBOX" } ?: ""),
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

package com.permutados.admin.ui.offers

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import com.permutados.admin.domain.model.OfferSummary
import com.permutados.admin.domain.model.Statuses
import com.permutados.admin.ui.common.EmptyText
import com.permutados.admin.ui.common.FilterRow
import com.permutados.admin.ui.common.ListCard
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.StatusBadge
import com.permutados.admin.ui.common.shortTime
import com.permutados.admin.ui.operations.OffersViewModel

@Composable
fun OffersScreen(vm: OffersViewModel, onOpenItem: (String) -> Unit) {
    val state by vm.state.collectAsState()
    val status by vm.status.collectAsState()
    Column {
        FilterRow(Statuses.OFFER, status, vm::onStatus)
        StateContent(state, vm::refresh) { offers ->
            if (offers.isEmpty()) EmptyText()
            LazyColumn { items(offers, key = { it.offerId }) { OfferRow(it, onOpenItem) } }
        }
    }
}

@Composable
fun OfferRow(o: OfferSummary, onOpenItem: (String) -> Unit) {
    ListCard(onClick = o.itemCode?.let { code -> { onOpenItem(code) } }) {
        Row {
            Text("${o.offerId} → ${o.itemCode ?: "?"}", Modifier.weight(1f))
            StatusBadge(o.status)
        }
        val what = if (o.type == "MONEY") "ARS ${o.price ?: "?"}" else "Permuta: ${o.offeredItem ?: "?"}"
        Text("${o.itemTitle ?: ""} · $what", style = MaterialTheme.typography.bodyMedium)
        Text(
            "Ofertante #${o.bidderId} · Dueño #${o.ownerId} · Creada ${shortTime(o.createdAt)} · Vence ${shortTime(o.expiresAt)}" +
                (o.closeReason?.let { " · Cierre: $it" } ?: ""),
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

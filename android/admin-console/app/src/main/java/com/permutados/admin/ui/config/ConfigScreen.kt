@file:OptIn(ExperimentalLayoutApi::class)

package com.permutados.admin.ui.config

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.permutados.admin.core.Role
import com.permutados.admin.domain.model.Statuses
import com.permutados.admin.ui.common.KeyValue
import com.permutados.admin.ui.common.MessageEffect
import com.permutados.admin.ui.common.SectionTitle
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.StatusBadge
import com.permutados.admin.ui.common.TextInputDialog
import kotlinx.serialization.json.JsonPrimitive

private val LABELS = mapOf(
    "contact_release_price" to "Precio por liberación de contactos",
    "contact_release_currency" to "Moneda",
    "offer_expiration_hours" to "Horas para responder una oferta",
    "payment_window_hours" to "Horas para pagar",
    "publication_expiration_days" to "Días de vigencia de una publicación",
    "publication_expiry_warning_days" to "Aviso de vencimiento (días antes)",
    "max_active_offers_per_bidder" to "Ofertas activas por ofertante",
)

@Composable
fun ConfigScreen(vm: ConfigViewModel, role: Role, snackbar: SnackbarHostState) {
    val state by vm.state.collectAsState()
    val edits by vm.edits.collectAsState()
    val message by vm.message.collectAsState()
    val busy by vm.busy.collectAsState()
    MessageEffect(message, snackbar, vm::consumeMessage)
    val canEdit = role.atLeast(Role.ADMIN)
    var pendingMode by remember { mutableStateOf<String?>(null) }

    StateContent(state, vm::refresh) { data ->
        Column(Modifier.verticalScroll(rememberScrollState()).padding(12.dp)) {
            SectionTitle("Modo del sistema")
            StatusBadge(data.mode)
            Text(
                "PAUSED: sin ofertas, aceptaciones, links de pago ni liberaciones nuevas (publicar y buscar siguen). " +
                    "EMERGENCY: se bloquea toda operación que cambie estado; los mensajes de liberación quedan retenidos. " +
                    "Los plazos se congelan y se extienden al volver a ONLINE.",
                style = MaterialTheme.typography.bodySmall,
            )
            if (canEdit) {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Statuses.SYSTEM_MODE.forEach { m ->
                        FilterChip(selected = data.mode == m, onClick = { if (m != data.mode) pendingMode = m }, label = { Text(m) }, enabled = !busy)
                    }
                }
            }

            SectionTitle("Configuración")
            if (!canEdit) Text("Solo lectura (requiere rol ADMIN).", style = MaterialTheme.typography.bodySmall)
            data.values.forEach { (key, value) ->
                val current = (value as? JsonPrimitive)?.content ?: value.toString()
                if (canEdit && value is JsonPrimitive) {
                    OutlinedTextField(
                        value = edits[key] ?: current,
                        onValueChange = { vm.edit(key, it) },
                        label = { Text(LABELS[key] ?: key) },
                        singleLine = true,
                        modifier = Modifier.fillMaxWidth().testTag("cfg_$key"),
                    )
                } else {
                    KeyValue(LABELS[key] ?: key, current)
                }
            }
            if (canEdit) {
                Row(Modifier.padding(top = 8.dp)) {
                    Button(onClick = { vm.save(data.values) }, enabled = edits.isNotEmpty() && !busy) { Text("Guardar cambios") }
                    Spacer(Modifier.padding(4.dp))
                    OutlinedButton(onClick = vm::discard, enabled = edits.isNotEmpty()) { Text("Descartar") }
                }
            }
        }
    }
    pendingMode?.let { m ->
        TextInputDialog("Cambiar a $m", "Motivo (se audita)", "Cambiar", onDismiss = { pendingMode = null }) {
            pendingMode = null
            vm.setMode(m, it)
        }
    }
}

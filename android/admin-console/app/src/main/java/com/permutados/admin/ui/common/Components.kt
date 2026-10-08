@file:OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)

package com.permutados.admin.ui.common

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.permutados.admin.core.ApiError
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

@Composable
fun LoadingBox(modifier: Modifier = Modifier) {
    Box(modifier.fillMaxSize().testTag("loading"), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
}

@Composable
fun ErrorBox(error: ApiError, onRetry: () -> Unit, modifier: Modifier = Modifier) {
    Column(
        modifier.fillMaxSize().padding(24.dp).testTag("error"),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(error.userMessage, color = MaterialTheme.colorScheme.error)
        Spacer(Modifier.padding(8.dp))
        Button(onClick = onRetry) { Text("Reintentar") }
    }
}

/** Renders Loading / Failed / Ready uniformly. */
@Composable
fun <T> StateContent(state: UiState<T>, onRetry: () -> Unit, content: @Composable (T) -> Unit) {
    when (state) {
        UiState.Loading -> LoadingBox()
        is UiState.Failed -> ErrorBox(state.error, onRetry)
        is UiState.Ready -> content(state.data)
    }
}

/** Shows a view-model message once in the snackbar. */
@Composable
fun MessageEffect(message: String?, snackbar: SnackbarHostState, consume: () -> Unit) {
    LaunchedEffect(message) {
        if (message != null) {
            snackbar.showSnackbar(message)
            consume()
        }
    }
}

@Composable
fun SectionTitle(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.titleMedium,
        fontWeight = FontWeight.SemiBold,
        modifier = Modifier.padding(top = 16.dp, bottom = 4.dp),
    )
}

@Composable
fun KeyValue(label: String, value: String?) {
    Row(Modifier.fillMaxWidth().padding(vertical = 2.dp)) {
        Text(label, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.width(150.dp))
        Text(value ?: "—", style = MaterialTheme.typography.bodyMedium)
    }
}

@Composable
fun StatusBadge(status: String) {
    val color = when (status) {
        "ACTIVE", "PUBLISHED", "APPROVED", "CONTACT_RELEASED", "ONLINE", "ok", "RESOLVED", "SUCCESS", "COMPLETED" -> Color(0xFF2E7D32)
        "SUSPENDED", "PENDING", "PAYMENT_PENDING", "RESERVED", "PENDING_REVIEW", "REMOVAL_PENDING", "PAUSED", "OPEN", "RETRY_PENDING" -> Color(0xFFB26A00)
        "BANNED", "FAILED", "REJECTED", "EMERGENCY", "error", "down", "NEEDS_REFUND" -> Color(0xFFC62828)
        else -> MaterialTheme.colorScheme.onSurfaceVariant
    }
    Text(status, color = color, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
}

@Composable
fun ListCard(onClick: (() -> Unit)? = null, content: @Composable () -> Unit) {
    Card(
        Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp)
            .let { if (onClick != null) it.clickable(onClick = onClick) else it },
    ) { Column(Modifier.padding(12.dp)) { content() } }
}

@Composable
fun FilterRow(options: List<String>, selected: String?, onSelect: (String?) -> Unit) {
    FlowRow(Modifier.padding(horizontal = 12.dp), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        FilterChip(selected = selected == null, onClick = { onSelect(null) }, label = { Text("Todos") })
        options.forEach { o -> FilterChip(selected = selected == o, onClick = { onSelect(o) }, label = { Text(o) }) }
    }
}

@Composable
fun SearchField(value: String, onValueChange: (String) -> Unit, label: String, onSearch: () -> Unit) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        singleLine = true,
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
        keyboardActions = KeyboardActions(onSearch = { onSearch() }),
        trailingIcon = { IconButton(onClick = onSearch) { Icon(Icons.Filled.Search, contentDescription = "Buscar") } },
        modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
    )
}

@Composable
fun EmptyText(text: String = "Sin resultados.") {
    Text(text, modifier = Modifier.padding(16.dp), color = MaterialTheme.colorScheme.onSurfaceVariant)
}

/** Generic, read-only renderer for backend reports with variable shape (metrics, overview, health...). */
@Composable
fun JsonTree(element: JsonElement?, modifier: Modifier = Modifier, depth: Int = 0) {
    Column(modifier) {
        when (element) {
            null, JsonNull -> Text("—")
            is JsonPrimitive -> Text(element.content, fontFamily = FontFamily.Monospace)
            is JsonObject -> element.forEach { (k, v) ->
                if (v is JsonPrimitive || v is JsonNull) {
                    KeyValue(k.replace('_', ' '), (v as? JsonPrimitive)?.content)
                } else {
                    Text(
                        k.replace('_', ' '),
                        style = if (depth == 0) MaterialTheme.typography.titleSmall else MaterialTheme.typography.labelLarge,
                        modifier = Modifier.padding(top = 6.dp),
                    )
                    JsonTree(v, Modifier.padding(start = 12.dp), depth + 1)
                }
            }
            is JsonArray -> if (element.isEmpty()) Text("(vacío)") else element.forEachIndexed { i, v ->
                if (i > 0) HorizontalDivider(Modifier.padding(vertical = 2.dp))
                JsonTree(v, Modifier, depth + 1)
            }
        }
    }
}

/** Dialog asking for a mandatory text (reason/notes); the backend audits it with the action. */
@Composable
fun TextInputDialog(
    title: String,
    label: String,
    confirm: String,
    minLength: Int = 3,
    initial: String = "",
    extra: (@Composable () -> Unit)? = null,
    onDismiss: () -> Unit,
    onConfirm: (String) -> Unit,
) {
    var text by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = {
            Column {
                OutlinedTextField(value = text, onValueChange = { text = it }, label = { Text(label) }, modifier = Modifier.testTag("dialog_input"))
                if (minLength > 0) Text("Mínimo $minLength caracteres.", style = MaterialTheme.typography.bodySmall)
                extra?.invoke()
            }
        },
        confirmButton = {
            TextButton(onClick = { onConfirm(text.trim()) }, enabled = text.trim().length >= minLength) { Text(confirm) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancelar") } },
    )
}

@Composable
fun ConfirmDialog(title: String, text: String, confirm: String, onDismiss: () -> Unit, onConfirm: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title) },
        text = { Text(text) },
        confirmButton = { TextButton(onClick = onConfirm) { Text(confirm) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancelar") } },
    )
}

private val SHORT = java.time.format.DateTimeFormatter.ofPattern("dd/MM HH:mm")

/** Backend timestamps are ISO-8601 with offset (UTC); shown in the device time zone. */
fun shortTime(iso: String?, zone: java.time.ZoneId = java.time.ZoneId.systemDefault()): String {
    if (iso.isNullOrBlank()) return "—"
    return try {
        java.time.OffsetDateTime.parse(iso).atZoneSameInstant(zone).format(SHORT)
    } catch (e: java.time.format.DateTimeParseException) {
        iso.replace('T', ' ').substringBefore('.').take(16)
    }
}

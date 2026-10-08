package com.permutados.admin.ui.config

import com.permutados.admin.core.ApiError
import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.repository.SystemRepository
import com.permutados.admin.ui.common.LoadViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.longOrNull

data class ConfigData(val values: JsonObject, val mode: String)

/**
 * Business configuration (price, currency, expirations, limits, channel knobs) and the system mode.
 * Values are validated again by the backend; this only converts text input to the JSON type of the current value.
 */
class ConfigViewModel(private val repo: SystemRepository) : LoadViewModel<ConfigData>() {
    private val _edits = MutableStateFlow<Map<String, String>>(emptyMap())
    val edits: StateFlow<Map<String, String>> = _edits.asStateFlow()

    init { refresh() }

    override suspend fun fetch(): Outcome<ConfigData> {
        val cfg = repo.config()
        if (cfg is Outcome.Failure) return cfg
        return when (val m = repo.mode()) {
            is Outcome.Failure -> m
            is Outcome.Success -> Outcome.Success(
                ConfigData(JsonObject((cfg as Outcome.Success).value.filterKeys { it != "system_mode" }), m.value),
            )
        }
    }

    fun edit(key: String, text: String) = _edits.update { it + (key to text) }
    fun discard() { _edits.value = emptyMap() }

    fun save(current: JsonObject) {
        val changes = mutableMapOf<String, JsonElement>()
        for ((key, text) in _edits.value) {
            val old = current[key] ?: continue
            val parsed = convert(old, text) ?: run {
                showMessage(ApiError.InvalidInput("Valor inválido para $key: \"$text\"").userMessage)
                return
            }
            if (parsed != old) changes[key] = parsed
        }
        if (changes.isEmpty()) {
            showMessage("No hay cambios.")
            return
        }
        action<JsonObject>("Configuración guardada (${changes.size}).", onSuccess = { _edits.value = emptyMap() }) {
            repo.updateConfig(changes)
        }
    }

    fun setMode(mode: String, reason: String) = action("Modo del sistema: $mode.") { repo.setMode(mode, reason) }

    companion object {
        /** Keeps the JSON type of the current value (bool / integer / decimal / string). */
        fun convert(old: JsonElement, text: String): JsonElement? {
            val t = text.trim()
            val p = old as? JsonPrimitive ?: return null
            return when {
                p.isString -> if (t.isEmpty()) null else JsonPrimitive(t)
                p.booleanOrNull != null -> t.lowercase().toBooleanStrictOrNull()?.let { JsonPrimitive(it) }
                p.longOrNull != null || p.doubleOrNull != null ->
                    t.toLongOrNull()?.let { JsonPrimitive(it) }
                        ?: t.replace(',', '.').toDoubleOrNull()?.let { JsonPrimitive(it) }
                else -> null
            }
        }
    }
}

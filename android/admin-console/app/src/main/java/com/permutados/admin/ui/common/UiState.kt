package com.permutados.admin.ui.common

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.permutados.admin.core.ApiError
import com.permutados.admin.core.Outcome
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

sealed interface UiState<out T> {
    data object Loading : UiState<Nothing>
    data class Ready<T>(val data: T) : UiState<T>
    data class Failed(val error: ApiError) : UiState<Nothing>
}

/**
 * Base for screens that load one backend resource and run actions on it. Actions show a message (success or the
 * mapped backend error) and reload the data, so the screen always reflects the backend state, never an optimistic
 * local guess.
 */
abstract class LoadViewModel<T> : ViewModel() {
    private val _state = MutableStateFlow<UiState<T>>(UiState.Loading)
    val state: StateFlow<UiState<T>> = _state.asStateFlow()

    private val _message = MutableStateFlow<String?>(null)
    val message: StateFlow<String?> = _message.asStateFlow()

    private val _busy = MutableStateFlow(false)
    val busy: StateFlow<Boolean> = _busy.asStateFlow()

    protected abstract suspend fun fetch(): Outcome<T>

    fun refresh() {
        viewModelScope.launch { load() }
    }

    protected suspend fun load() {
        if (_state.value !is UiState.Ready) _state.value = UiState.Loading
        _state.value = when (val r = fetch()) {
            is Outcome.Success -> UiState.Ready(r.value)
            is Outcome.Failure -> UiState.Failed(r.error)
        }
    }

    /** Runs a mutating call; on success shows [success] and reloads. */
    protected fun <R> action(success: String, reload: Boolean = true, onSuccess: (R) -> Unit = {}, block: suspend () -> Outcome<R>) {
        if (_busy.value) return
        viewModelScope.launch {
            _busy.value = true
            when (val r = block()) {
                is Outcome.Success -> {
                    onSuccess(r.value)
                    _message.value = success
                    if (reload) load()
                }
                is Outcome.Failure -> _message.value = r.error.userMessage
            }
            _busy.value = false
        }
    }

    protected fun showMessage(text: String) {
        _message.value = text
    }

    fun consumeMessage() {
        _message.value = null
    }
}

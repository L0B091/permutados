package com.permutados.admin.ui.login

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.permutados.admin.core.Outcome
import com.permutados.admin.domain.repository.AuthRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

data class LoginUiState(
    val baseUrl: String = "",
    val username: String = "",
    val password: String = "",
    val loading: Boolean = false,
    val error: String? = null,
    val loggedIn: Boolean = false,
)

class LoginViewModel(private val repo: AuthRepository, defaultBaseUrl: String) : ViewModel() {
    private val _state = MutableStateFlow(LoginUiState(baseUrl = defaultBaseUrl))
    val state: StateFlow<LoginUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            repo.lastBaseUrl()?.let { url -> _state.update { if (it.baseUrl == defaultBaseUrl) it.copy(baseUrl = url) else it } }
        }
    }

    fun onBaseUrl(v: String) = _state.update { it.copy(baseUrl = v, error = null) }
    fun onUsername(v: String) = _state.update { it.copy(username = v, error = null) }
    fun onPassword(v: String) = _state.update { it.copy(password = v, error = null) }

    fun login() {
        val s = _state.value
        if (s.loading) return
        _state.update { it.copy(loading = true, error = null) }
        viewModelScope.launch {
            when (val r = repo.login(s.baseUrl, s.username, s.password)) {
                // The password is dropped from memory as soon as the call returns.
                is Outcome.Success -> _state.update { it.copy(loading = false, password = "", loggedIn = true) }
                is Outcome.Failure -> _state.update { it.copy(loading = false, password = "", error = r.error.userMessage) }
            }
        }
    }
}

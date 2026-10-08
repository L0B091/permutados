package com.permutados.admin.ui

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.permutados.admin.core.ApiError
import com.permutados.admin.ui.common.StateContent
import com.permutados.admin.ui.common.UiState
import com.permutados.admin.ui.login.LoginContent
import com.permutados.admin.ui.login.LoginUiState
import com.permutados.admin.ui.theme.PermutadosTheme
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.annotation.Config

/** Compose UI on the JVM (Robolectric). */
@RunWith(AndroidJUnit4::class)
@Config(sdk = [34], application = Application::class)
class LoginScreenTest {
    @get:Rule val compose = createComposeRule()

    @Test fun loginButtonEnablesOnlyWithCredentialsAndSubmits() {
        var state by mutableStateOf(LoginUiState(baseUrl = "https://api.permutados.example/"))
        var submitted = 0
        compose.setContent {
            PermutadosTheme {
                LoginContent(
                    state,
                    onBaseUrl = { state = state.copy(baseUrl = it) },
                    onUsername = { state = state.copy(username = it) },
                    onPassword = { state = state.copy(password = it) },
                    onLogin = { submitted++ },
                )
            }
        }
        compose.onNodeWithTag("login_button").assertIsNotEnabled()
        compose.onNodeWithTag("login_user").performTextInput("ana")
        compose.onNodeWithTag("login_button").assertIsNotEnabled()
        compose.onNodeWithTag("login_password").performTextInput("s3cret")
        compose.onNodeWithTag("login_button").assertIsEnabled().performClick()
        assertEquals(1, submitted)
        assertEquals("s3cret", state.password)
        // The password field is masked.
        compose.onNodeWithText("s3cret").assertDoesNotExist()
    }

    @Test fun errorIsDisplayed() {
        compose.setContent {
            PermutadosTheme { LoginContent(LoginUiState(error = ApiError.Unauthorized.userMessage), {}, {}, {}, {}) }
        }
        compose.onNodeWithTag("login_error").assertIsDisplayed()
        compose.onNodeWithText(ApiError.Unauthorized.userMessage).assertIsDisplayed()
    }

    @Test fun failedStateOffersRetry() {
        var retries = 0
        compose.setContent {
            PermutadosTheme {
                StateContent<String>(UiState.Failed(ApiError.Network), onRetry = { retries++ }) { }
            }
        }
        compose.onNodeWithText(ApiError.Network.userMessage).assertIsDisplayed()
        compose.onNodeWithText("Reintentar").performClick()
        assertEquals(1, retries)
    }
}

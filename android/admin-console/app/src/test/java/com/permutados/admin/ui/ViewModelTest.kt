package com.permutados.admin.ui

import com.permutados.admin.core.ApiError
import com.permutados.admin.core.Outcome
import com.permutados.admin.testutil.FakeAuthRepository
import com.permutados.admin.testutil.FakeChannelRepository
import com.permutados.admin.testutil.FakePublicationsRepository
import com.permutados.admin.testutil.FakeSystemRepository
import com.permutados.admin.testutil.FakeUsersRepository
import com.permutados.admin.testutil.MainDispatcherRule
import com.permutados.admin.testutil.session
import com.permutados.admin.ui.channel.ChannelPostsViewModel
import com.permutados.admin.ui.channel.SuggestionsViewModel
import com.permutados.admin.ui.common.UiState
import com.permutados.admin.ui.config.ConfigViewModel
import com.permutados.admin.ui.items.ItemDetailViewModel
import com.permutados.admin.ui.login.LoginViewModel
import com.permutados.admin.ui.users.UserDetailViewModel
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test

class ViewModelTest {
    @get:Rule val main = MainDispatcherRule()

    @Test fun loginSuccessDropsPasswordFromState() {
        val repo = FakeAuthRepository(Outcome.Success(session("https://api.permutados.example/")))
        val vm = LoginViewModel(repo, "https://default.example/")
        assertEquals("prefilled with the last used URL", "https://saved.permutados.example/", vm.state.value.baseUrl)
        vm.onUsername("ana"); vm.onPassword("s3cret"); vm.login()
        assertEquals("s3cret", repo.calls.single().third)
        assertTrue(vm.state.value.loggedIn)
        assertEquals("", vm.state.value.password)
        assertNull(vm.state.value.error)
    }

    @Test fun loginFailureShowsMappedError() {
        val vm = LoginViewModel(FakeAuthRepository(Outcome.Failure(ApiError.RateLimited)), "https://d.example/")
        vm.onUsername("ana"); vm.onPassword("x"); vm.login()
        assertEquals(ApiError.RateLimited.userMessage, vm.state.value.error)
        assertEquals(false, vm.state.value.loggedIn)
    }

    @Test fun suspendingAUserReloadsFromBackend() {
        val repo = FakeUsersRepository()
        val vm = UserDetailViewModel(repo, 5)
        assertEquals("ACTIVE", (vm.state.value as UiState.Ready).data.status)
        vm.suspendUser("spam reiterado")
        assertEquals(listOf("suspend:spam reiterado"), repo.actions)
        assertEquals("SUSPENDED", (vm.state.value as UiState.Ready).data.status)
        assertEquals("Usuario suspendido.", vm.message.value)
        assertEquals(2, repo.loads)
        vm.consumeMessage()
        assertNull(vm.message.value)
    }

    @Test fun failedActionShowsErrorAndKeepsData() {
        val repo = FakeUsersRepository().apply { actionError = ApiError.Forbidden }
        val vm = UserDetailViewModel(repo, 5)
        vm.ban("confirmado")
        assertEquals(ApiError.Forbidden.userMessage, vm.message.value)
        assertEquals("ACTIVE", (vm.state.value as UiState.Ready).data.status)
        assertEquals(false, vm.busy.value)
    }

    @Test fun itemEditSendsOnlyChangedFields() {
        val repo = FakePublicationsRepository()
        val vm = ItemDetailViewModel(repo, "ITEM-ABCDE")
        val current = (vm.state.value as UiState.Ready).data
        vm.edit(current, title = "Bicicleta", category = "deportes", description = "Rodado 29", reason = "título confuso")
        val req = repo.edits.single()
        assertEquals("Bicicleta", req.title)
        assertNull(req.category)
        assertNull(req.description)
        assertEquals("título confuso", req.reason)
    }

    @Test fun itemEditWithoutChangesDoesNotCallBackend() {
        val repo = FakePublicationsRepository()
        val vm = ItemDetailViewModel(repo, "ITEM-ABCDE")
        val current = (vm.state.value as UiState.Ready).data
        vm.edit(current, "Bici", "deportes", "Rodado 29", "nada")
        assertTrue(repo.edits.isEmpty())
        assertTrue(vm.message.value!!.contains("No hay cambios"))
    }

    @Test fun contactDataRejectionIsShown() {
        val repo = FakePublicationsRepository().apply {
            editError = ApiError.Validation("CONTACT_DATA_IN_TEXT", "the new text contains contact data")
        }
        val vm = ItemDetailViewModel(repo, "ITEM-ABCDE")
        vm.edit((vm.state.value as UiState.Ready).data, "llamame al 1122334455", "deportes", "Rodado 29", "test")
        assertTrue(vm.message.value!!.contains("CONTACT_DATA_IN_TEXT"))
    }

    @Test fun scoreIsLoadedOnDemand() {
        val vm = ItemDetailViewModel(FakePublicationsRepository(), "ITEM-ABCDE")
        assertNull(vm.score.value)
        vm.loadScore()
        assertEquals(JsonPrimitive(71), vm.score.value!!["score"])
    }

    @Test fun configSaveConvertsTypesAndSendsOnlyChanges() {
        val repo = FakeSystemRepository()
        val vm = ConfigViewModel(repo)
        val data = (vm.state.value as UiState.Ready).data
        assertTrue("system_mode is managed separately", "system_mode" !in data.values)
        vm.edit("offer_expiration_hours", "48")
        vm.edit("channel_enabled", "false")
        vm.edit("contact_release_currency", "ARS") // unchanged
        vm.save(data.values)
        val sent = repo.updates.single()
        assertEquals(setOf("offer_expiration_hours", "channel_enabled"), sent.keys)
        assertEquals(JsonPrimitive(48L), sent["offer_expiration_hours"])
        assertEquals(JsonPrimitive(false), sent["channel_enabled"])
        assertTrue(vm.edits.value.isEmpty())
    }

    @Test fun configRejectsWrongTypeLocally() {
        val repo = FakeSystemRepository()
        val vm = ConfigViewModel(repo)
        val data = (vm.state.value as UiState.Ready).data
        vm.edit("offer_expiration_hours", "mañana")
        vm.save(data.values)
        assertTrue(repo.updates.isEmpty())
        assertTrue(vm.message.value!!.contains("offer_expiration_hours"))
    }

    @Test fun systemModeChangeCarriesReason() {
        val repo = FakeSystemRepository()
        val vm = ConfigViewModel(repo)
        vm.setMode("PAUSED", "mantenimiento de la base")
        assertEquals("PAUSED" to "mantenimiento de la base", repo.modeChanges.single())
        assertEquals("PAUSED", (vm.state.value as UiState.Ready).data.mode)
    }

    @Test fun priceAcceptsDecimalInput() {
        assertEquals(JsonPrimitive(3500.5), ConfigViewModel.convert(JsonPrimitive(3000), "3500,5"))
        assertEquals(JsonPrimitive(3500L), ConfigViewModel.convert(JsonPrimitive(3000), "3500"))
        assertNull(ConfigViewModel.convert(JsonPrimitive(true), "quizás"))
    }

    @Test fun channelSuggestionsQueueSelection() {
        val repo = FakeChannelRepository()
        val vm = SuggestionsViewModel(repo)
        vm.queueSelected() // nothing selected -> no call
        assertTrue(repo.queued.isEmpty())
        vm.toggle("ITEM-FGHJK"); vm.toggle("ITEM-ABCDE"); vm.toggle("ITEM-ABCDE")
        vm.queueSelected()
        assertEquals(listOf("ITEM-FGHJK"), repo.queued.single())
        assertTrue(vm.selected.value.isEmpty())
    }

    @Test fun channelPostLifecycleActions() {
        val repo = FakeChannelRepository()
        val vm = ChannelPostsViewModel(repo)
        vm.markPublished(1, "https://whatsapp.com/channel/x/1")
        vm.requestRemoval(1, "SOLD")
        vm.confirmRemoved(1)
        vm.register("ITEM-ABCDE", null)
        assertEquals(
            listOf("published:1:https://whatsapp.com/channel/x/1", "remove:1:SOLD", "removed:1", "register:ITEM-ABCDE"),
            repo.actions,
        )
    }
}

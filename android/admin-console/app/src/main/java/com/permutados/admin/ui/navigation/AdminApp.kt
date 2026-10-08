@file:OptIn(ExperimentalMaterial3Api::class)

package com.permutados.admin.ui.navigation

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.ExitToApp
import androidx.compose.material.icons.automirrored.filled.List
import androidx.compose.material.icons.filled.Build
import androidx.compose.material.icons.filled.CheckCircle
import androidx.compose.material.icons.filled.Done
import androidx.compose.material.icons.filled.Email
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.Lock
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Person
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.Share
import androidx.compose.material.icons.filled.ShoppingCart
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.unit.dp
import androidx.navigation.NavHostController
import androidx.navigation.NavType
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import androidx.navigation.navArgument
import com.permutados.admin.AppContainer
import com.permutados.admin.core.Role
import com.permutados.admin.data.session.Session
import com.permutados.admin.ui.channel.ChannelScreen
import com.permutados.admin.ui.common.LoadingBox
import com.permutados.admin.ui.common.appViewModel
import com.permutados.admin.ui.config.ConfigScreen
import com.permutados.admin.ui.config.ConfigViewModel
import com.permutados.admin.ui.conversations.ConversationDetailScreen
import com.permutados.admin.ui.conversations.ConversationDetailViewModel
import com.permutados.admin.ui.conversations.ConversationsScreen
import com.permutados.admin.ui.conversations.ConversationsViewModel
import com.permutados.admin.ui.dashboard.DashboardScreen
import com.permutados.admin.ui.dashboard.DashboardViewModel
import com.permutados.admin.ui.items.ItemDetailScreen
import com.permutados.admin.ui.items.ItemDetailViewModel
import com.permutados.admin.ui.items.ItemsScreen
import com.permutados.admin.ui.items.ItemsViewModel
import com.permutados.admin.ui.login.LoginScreen
import com.permutados.admin.ui.login.LoginViewModel
import com.permutados.admin.ui.offers.OffersScreen
import com.permutados.admin.ui.operations.OffersViewModel
import com.permutados.admin.ui.operations.OperationDetailScreen
import com.permutados.admin.ui.operations.OperationDetailViewModel
import com.permutados.admin.ui.operations.OperationsScreen
import com.permutados.admin.ui.operations.OperationsViewModel
import com.permutados.admin.ui.operations.PaymentsScreen
import com.permutados.admin.ui.operations.PaymentsViewModel
import com.permutados.admin.ui.security.SecurityScreen
import com.permutados.admin.ui.system.SystemScreen
import com.permutados.admin.ui.system.SystemViewModel
import com.permutados.admin.ui.users.UserDetailScreen
import com.permutados.admin.ui.users.UserDetailViewModel
import com.permutados.admin.ui.users.UsersScreen
import com.permutados.admin.ui.users.UsersViewModel
import kotlinx.coroutines.launch

/** Top-level sections (spec section 8). `minRole` hides sections the backend would reject anyway. */
enum class Section(val route: String, val label: String, val icon: ImageVector, val minRole: Role = Role.VIEWER) {
    DASHBOARD("dashboard", "Dashboard", Icons.Filled.Home),
    USERS("users", "Usuarios", Icons.Filled.Person),
    ITEMS("items", "Publicaciones", Icons.AutoMirrored.Filled.List),
    OFFERS("offers", "Ofertas", Icons.Filled.ShoppingCart),
    OPERATIONS("operations", "Operaciones", Icons.Filled.CheckCircle),
    PAYMENTS("payments", "Pagos", Icons.Filled.Done),
    CONVERSATIONS("conversations", "Conversaciones", Icons.Filled.Email, Role.MODERATOR),
    SECURITY("security", "Seguridad", Icons.Filled.Lock),
    CHANNEL("channel", "Canal", Icons.Filled.Share),
    CONFIG("config", "Configuración", Icons.Filled.Settings),
    SYSTEM("system", "Sistema", Icons.Filled.Build),
}

private val DETAIL_TITLES = mapOf(
    "users/{id}" to "Usuario",
    "items/{code}" to "Publicación",
    "operations/{code}" to "Operación",
    "conversations/{id}" to "Conversación",
)

@Composable
fun AdminApp(container: AppContainer) {
    val ready by container.sessionStore.ready.collectAsState()
    val session by container.sessionStore.session.collectAsState()
    when {
        !ready -> LoadingBox()
        session == null -> LoginScreen(appViewModel { LoginViewModel(it.authRepository, it.defaultBaseUrl) })
        else -> MainShell(container, session!!)
    }
}

@Composable
private fun MainShell(container: AppContainer, session: Session) {
    val nav = rememberNavController()
    val drawer = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    val snackbar = remember { SnackbarHostState() }
    val role = session.roleEnum
    val entry by nav.currentBackStackEntryAsState()
    val route = entry?.destination?.route ?: Section.DASHBOARD.route
    val isDetail = route in DETAIL_TITLES

    ModalNavigationDrawer(
        drawerState = drawer,
        drawerContent = {
            ModalDrawerSheet {
                Column(Modifier.verticalScroll(rememberScrollState())) {
                    Text("PERMUTADOS Admin", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(16.dp))
                    Text("${session.username} · ${session.role}", modifier = Modifier.padding(horizontal = 16.dp))
                    Text(session.baseUrl, style = MaterialTheme.typography.bodySmall, modifier = Modifier.padding(horizontal = 16.dp))
                    HorizontalDivider(Modifier.padding(vertical = 8.dp))
                    Section.entries.filter { role.atLeast(it.minRole) }.forEach { s ->
                        NavigationDrawerItem(
                            label = { Text(s.label) },
                            icon = { Icon(s.icon, contentDescription = null) },
                            selected = route == s.route,
                            onClick = {
                                scope.launch { drawer.close() }
                                nav.navigate(s.route) {
                                    popUpTo(Section.DASHBOARD.route) { saveState = true }
                                    launchSingleTop = true
                                    restoreState = true
                                }
                            },
                            modifier = Modifier.padding(horizontal = 12.dp),
                        )
                    }
                    HorizontalDivider(Modifier.padding(vertical = 8.dp))
                    NavigationDrawerItem(
                        label = { Text("Cerrar sesión") },
                        icon = { Icon(Icons.AutoMirrored.Filled.ExitToApp, contentDescription = null) },
                        selected = false,
                        onClick = { scope.launch { container.authRepository.logout() } },
                        modifier = Modifier.padding(horizontal = 12.dp),
                    )
                }
            }
        },
    ) {
        Scaffold(
            topBar = {
                TopAppBar(
                    title = { Text(DETAIL_TITLES[route] ?: Section.entries.firstOrNull { it.route == route }?.label ?: "") },
                    navigationIcon = {
                        if (isDetail) {
                            IconButton(onClick = { nav.popBackStack() }) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Volver") }
                        } else {
                            IconButton(onClick = { scope.launch { drawer.open() } }) { Icon(Icons.Filled.Menu, "Menú") }
                        }
                    },
                )
            },
            snackbarHost = { SnackbarHost(snackbar) },
        ) { padding ->
            AdminNavHost(nav, role, snackbar, Modifier.padding(padding))
        }
    }
}

@Composable
private fun AdminNavHost(nav: NavHostController, role: Role, snackbar: SnackbarHostState, modifier: Modifier) {
    val openUser: (Long) -> Unit = { nav.navigate("users/$it") }
    val openItem: (String) -> Unit = { nav.navigate("items/$it") }
    val openOperation: (String) -> Unit = { nav.navigate("operations/$it") }
    val openConversation: (Long) -> Unit = { nav.navigate("conversations/$it") }

    NavHost(nav, startDestination = Section.DASHBOARD.route, modifier = modifier) {
        composable(Section.DASHBOARD.route) { DashboardScreen(appViewModel { DashboardViewModel(it.adminRepository) }) }
        composable(Section.USERS.route) { UsersScreen(appViewModel { UsersViewModel(it.adminRepository) }, openUser) }
        composable("users/{id}", listOf(navArgument("id") { type = NavType.LongType })) { e ->
            val id = e.arguments?.getLong("id") ?: 0L
            UserDetailScreen(appViewModel("user-$id") { UserDetailViewModel(it.adminRepository, id) }, role, snackbar, openItem, openConversation)
        }
        composable(Section.ITEMS.route) { ItemsScreen(appViewModel { ItemsViewModel(it.adminRepository) }, openItem) }
        composable("items/{code}") { e ->
            val code = e.arguments?.getString("code").orEmpty()
            ItemDetailScreen(appViewModel("item-$code") { ItemDetailViewModel(it.adminRepository, code) }, role, snackbar, openUser, openItem)
        }
        composable(Section.OFFERS.route) { OffersScreen(appViewModel { OffersViewModel(it.adminRepository) }, openItem) }
        composable(Section.OPERATIONS.route) { OperationsScreen(appViewModel { OperationsViewModel(it.adminRepository) }, openOperation) }
        composable("operations/{code}") { e ->
            val code = e.arguments?.getString("code").orEmpty()
            OperationDetailScreen(appViewModel("op-$code") { OperationDetailViewModel(it.adminRepository, code) }, openUser, openItem)
        }
        composable(Section.PAYMENTS.route) { PaymentsScreen(appViewModel { PaymentsViewModel(it.adminRepository) }, openOperation) }
        composable(Section.CONVERSATIONS.route) {
            ConversationsScreen(appViewModel { ConversationsViewModel(it.adminRepository) }, openConversation)
        }
        composable("conversations/{id}", listOf(navArgument("id") { type = NavType.LongType })) { e ->
            val id = e.arguments?.getLong("id") ?: 0L
            ConversationDetailScreen(appViewModel("conv-$id") { ConversationDetailViewModel(it.adminRepository, id) }, openUser)
        }
        composable(Section.SECURITY.route) { SecurityScreen(role, snackbar, openUser) }
        composable(Section.CHANNEL.route) { ChannelScreen(role, snackbar, openItem) }
        composable(Section.CONFIG.route) { ConfigScreen(appViewModel { ConfigViewModel(it.adminRepository) }, role, snackbar) }
        composable(Section.SYSTEM.route) { SystemScreen(appViewModel { SystemViewModel(it.adminRepository) }) }
    }
}

package com.permutados.admin

import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.permutados.admin.ui.navigation.AdminApp
import com.permutados.admin.ui.theme.PermutadosTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Admin data (masked PII, conversations) must not leak into screenshots or the recent-apps thumbnail.
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)
        enableEdgeToEdge()
        val container = (application as PermutadosAdminApp).container
        setContent { PermutadosTheme { AdminApp(container) } }
    }
}

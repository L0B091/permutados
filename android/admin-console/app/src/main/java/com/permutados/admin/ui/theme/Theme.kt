package com.permutados.admin.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val Brand = Color(0xFF1B6E53)
private val Light = lightColorScheme(primary = Brand, secondary = Color(0xFF4E6359), tertiary = Color(0xFF3D6373))
private val Dark = darkColorScheme(primary = Color(0xFF8BD6B6), secondary = Color(0xFFB5CCC0), tertiary = Color(0xFFA5CDDF))

@Composable
fun PermutadosTheme(dark: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (dark) Dark else Light, content = content)
}

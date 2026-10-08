package com.permutados.admin.core

/** Mirrors the backend RBAC (VIEWER < MODERATOR < ADMIN < SUPERADMIN). The backend is the authority; the app only
 * hides actions the role cannot perform. */
enum class Role(val rank: Int) {
    VIEWER(0), MODERATOR(1), ADMIN(2), SUPERADMIN(3);

    fun atLeast(other: Role): Boolean = rank >= other.rank

    companion object {
        fun parse(value: String?): Role = entries.firstOrNull { it.name == value?.uppercase() } ?: VIEWER
    }
}

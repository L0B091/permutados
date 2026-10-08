package com.permutados.admin.ui

import com.permutados.admin.ui.common.shortTime
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.ZoneId

class FormattingTest {
    @Test fun backendUtcTimesAreShownInLocalZone() {
        val ba = ZoneId.of("America/Argentina/Buenos_Aires")
        assertEquals("08/10 09:30", shortTime("2026-10-08T12:30:00.123456+00:00", ba))
        assertEquals("—", shortTime(null, ba))
        assertEquals("2026-10-08 12:30", shortTime("2026-10-08T12:30:00", ba)) // no offset: shown as-is
    }
}

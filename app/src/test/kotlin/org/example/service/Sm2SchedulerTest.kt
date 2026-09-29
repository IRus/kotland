package org.example.service

import org.example.model.ReviewGrade
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.time.Instant

class Sm2SchedulerTest {
    private val scheduler = Sm2Scheduler()

    @Test
    fun `first good answer creates one day interval`() {
        val now = Instant.parse("2026-03-03T10:00:00Z")
        val result = scheduler.schedule(ReviewStateInput(), ReviewGrade.GOOD, now)

        assertEquals(1, result.intervalDays)
        assertEquals(1, result.repetition)
        assertEquals(0, result.lapseCount)
        assertTrue(result.easeFactor >= 1.3)
        assertEquals("2026-03-04T10:00:00Z", result.dueAt.toString())
    }

    @Test
    fun `second good answer jumps to six days`() {
        val now = Instant.parse("2026-03-03T10:00:00Z")
        val previous = ReviewStateInput(easeFactor = 2.5, intervalDays = 1, repetition = 1, lapseCount = 0)
        val result = scheduler.schedule(previous, ReviewGrade.GOOD, now)

        assertEquals(6, result.intervalDays)
        assertEquals(2, result.repetition)
        assertEquals("2026-03-09T10:00:00Z", result.dueAt.toString())
    }

    @Test
    fun `again resets repetition and increments lapses`() {
        val now = Instant.parse("2026-03-03T10:00:00Z")
        val previous = ReviewStateInput(easeFactor = 2.5, intervalDays = 16, repetition = 4, lapseCount = 2)
        val result = scheduler.schedule(previous, ReviewGrade.AGAIN, now)

        assertEquals(1, result.intervalDays)
        assertEquals(0, result.repetition)
        assertEquals(3, result.lapseCount)
    }

    @Test
    fun `easy extends interval more than good`() {
        val now = Instant.parse("2026-03-03T10:00:00Z")
        val previous = ReviewStateInput(easeFactor = 2.4, intervalDays = 10, repetition = 3, lapseCount = 0)

        val good = scheduler.schedule(previous, ReviewGrade.GOOD, now)
        val easy = scheduler.schedule(previous, ReviewGrade.EASY, now)

        assertTrue(easy.intervalDays > good.intervalDays)
    }
}

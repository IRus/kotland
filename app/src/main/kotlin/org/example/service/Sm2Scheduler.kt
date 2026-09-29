package org.example.service

import org.example.model.ReviewGrade
import java.time.Instant
import java.time.temporal.ChronoUnit
import kotlin.math.max
import kotlin.math.roundToInt

data class ReviewStateInput(
    val easeFactor: Double = 2.5,
    val intervalDays: Int = 0,
    val repetition: Int = 0,
    val lapseCount: Int = 0,
)

data class ReviewStateOutput(
    val easeFactor: Double,
    val intervalDays: Int,
    val repetition: Int,
    val lapseCount: Int,
    val dueAt: Instant,
)

class Sm2Scheduler {
    fun schedule(current: ReviewStateInput, grade: ReviewGrade, now: Instant = Instant.now()): ReviewStateOutput {
        val quality = toQuality(grade)

        var ease = current.easeFactor
        var repetition = current.repetition
        var interval = current.intervalDays
        var lapseCount = current.lapseCount

        ease = max(1.3, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))

        if (quality < 3) {
            repetition = 0
            interval = 1
            lapseCount += 1
        } else {
            repetition += 1
            interval = when (repetition) {
                1 -> 1
                2 -> 6
                else -> max(1, (interval * ease).roundToInt())
            }

            if (grade == ReviewGrade.EASY) {
                interval = max(1, (interval * 1.3).roundToInt())
            }
        }

        val dueAt = now.plus(interval.toLong(), ChronoUnit.DAYS)

        return ReviewStateOutput(
            easeFactor = ease,
            intervalDays = interval,
            repetition = repetition,
            lapseCount = lapseCount,
            dueAt = dueAt,
        )
    }

    private fun toQuality(grade: ReviewGrade): Int = when (grade) {
        ReviewGrade.AGAIN -> 1
        ReviewGrade.HARD -> 3
        ReviewGrade.GOOD -> 4
        ReviewGrade.EASY -> 5
    }
}

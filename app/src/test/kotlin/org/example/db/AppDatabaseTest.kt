package org.example.db

import org.example.config.AppConfig
import org.example.model.AudioQuality
import org.example.model.AudioSource
import org.example.model.CardType
import org.example.model.ReviewAnswerRequest
import org.example.model.ReviewGrade
import org.example.service.Sm2Scheduler
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertNotNull
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.nio.file.Files
import kotlin.io.path.Path

class AppDatabaseTest {
    @Test
    fun `upsert card is idempotent and preserves review scheduling`() {
        val tempRoot = Files.createTempDirectory("kotland-db-test")
        val dbPath = tempRoot.resolve("app.sqlite")
        val mediaRoot = tempRoot.resolve("media")

        val config = AppConfig(
            host = "127.0.0.1",
            port = 0,
            dbPath = dbPath,
            mediaRoot = mediaRoot,
            googleClientId = "",
            googleClientSecret = "",
            googleRedirectUri = "",
            sessionSecret = "test-secret",
            sessionSecure = false,
            adminEmails = emptySet(),
        )

        AppDatabase(config).use { db ->
            val user = db.upsertUser("user@example.com", "User", "user")
            assertEquals("user@example.com", user.email)

            val deckId = db.upsertDeck("polish-core", "Polish Core")

            val firstId = db.upsertCard(
                UpsertCardInput(
                    deckId = deckId,
                    type = CardType.WORD,
                    canonicalKey = "WORD|kot|кот",
                    frontTextPl = "kot",
                    backRuTranslation = "кот",
                    tags = listOf("animal"),
                )
            )

            db.upsertAudio(
                firstId,
                UpsertAudioInput(
                    source = AudioSource.TTS_GOOGLE,
                    label = "TTS (Google)",
                    path = "audio/tts-google/kot.mp3",
                    quality = AudioQuality.AUTO,
                    isPrimary = true,
                )
            )

            val review = db.applyReview(
                email = user.email,
                request = ReviewAnswerRequest(cardId = firstId, grade = ReviewGrade.GOOD, responseMs = 1200),
                scheduler = Sm2Scheduler(),
            )
            assertTrue(review.intervalDays >= 1)

            val secondId = db.upsertCard(
                UpsertCardInput(
                    deckId = deckId,
                    type = CardType.WORD,
                    canonicalKey = "WORD|kot|кот",
                    frontTextPl = "kot",
                    backRuTranslation = "кот",
                    backRuPronHint = "русское чтение",
                    tags = listOf("animal", "core"),
                )
            )

            assertEquals(firstId, secondId)

            val card = db.getCard(firstId)
            assertNotNull(card)
            assertTrue(card!!.tags.contains("core"))
            assertEquals(1, card.audioVariants.size)

            val dueNow = db.listDecksWithDue(user.email)
                .first { it.code == "polish-core" }
                .dueCount
            assertEquals(0, dueNow)

            val reset = db.resetDeckProgress(user.email, "polish-core")
            assertEquals(MutationStatus.OK, reset.status)
            assertEquals(1, reset.value)

            val dueAfterReset = db.listDecksWithDue(user.email)
                .first { it.code == "polish-core" }
                .dueCount
            assertEquals(1, dueAfterReset)
        }
    }

    @Test
    fun `user can create and manage personal deck`() {
        val tempRoot = Files.createTempDirectory("kotland-db-manage-test")
        val dbPath = tempRoot.resolve("app.sqlite")
        val mediaRoot = tempRoot.resolve("media")

        val config = AppConfig(
            host = "127.0.0.1",
            port = 0,
            dbPath = dbPath,
            mediaRoot = mediaRoot,
            googleClientId = "",
            googleClientSecret = "",
            googleRedirectUri = "",
            sessionSecret = "test-secret",
            sessionSecure = false,
            adminEmails = emptySet(),
        )

        AppDatabase(config).use { db ->
            val user = db.upsertUser("owner@example.com", "Owner", "user")

            val createdDeck = db.createDeckForUser(user.email, "my-deck", "My Deck")
            assertEquals(MutationStatus.OK, createdDeck.status)
            assertNotNull(createdDeck.value)
            val deck = createdDeck.value ?: error("Deck should be created")
            assertTrue(deck.canEdit)

            val systemDeck = db.listManageDecks(user.email).first { it.code == "history" }
            assertFalse(systemDeck.canEdit)
            val forbiddenDeckUpdate = db.updateDeckForUser(user.email, systemDeck.id, "history", "New title")
            assertEquals(MutationStatus.FORBIDDEN, forbiddenDeckUpdate.status)

            val createCard = db.createCardForDeck(
                user.email,
                deck.id,
                ManageCardInput(
                    type = CardType.PHRASE,
                    frontTextPl = "dzień dobry",
                    frontTranscript = "джень добры",
                    backRuTranslation = "добрый день",
                    tags = listOf("greeting"),
                ),
            )
            assertEquals(MutationStatus.OK, createCard.status)
            val cardId = createCard.value!!.id

            val listedCards = db.listDeckCardsForManage(user.email, deck.id)
            assertEquals(MutationStatus.OK, listedCards.status)
            assertEquals(1, listedCards.value!!.size)

            val updatedCard = db.updateManagedCard(
                user.email,
                cardId,
                ManageCardInput(
                    type = CardType.PHRASE,
                    frontTextPl = "dzień dobry!",
                    backRuTranslation = "добрый день!",
                    tags = listOf("greeting", "basic"),
                ),
            )
            assertEquals(MutationStatus.OK, updatedCard.status)
            assertEquals("dzień dobry!", updatedCard.value!!.frontTextPl)

            assertEquals(MutationStatus.OK, db.deleteManagedCard(user.email, cardId))
            assertEquals(MutationStatus.OK, db.deleteDeckForUser(user.email, deck.id))
        }
    }

    @Test
    fun `user can manage imported non-system deck with no owner`() {
        val tempRoot = Files.createTempDirectory("kotland-db-shared-import-test")
        val dbPath = tempRoot.resolve("app.sqlite")
        val mediaRoot = tempRoot.resolve("media")

        val config = AppConfig(
            host = "127.0.0.1",
            port = 0,
            dbPath = dbPath,
            mediaRoot = mediaRoot,
            googleClientId = "",
            googleClientSecret = "",
            googleRedirectUri = "",
            sessionSecret = "test-secret",
            sessionSecure = false,
            adminEmails = emptySet(),
        )

        AppDatabase(config).use { db ->
            val user = db.upsertUser("owner@example.com", "Owner", "user")
            val importedDeckId = db.upsertDeck("shared-import", "Shared Import")

            val manageDeck = db.listManageDecks(user.email).first { it.code == "shared-import" }
            assertTrue(manageDeck.canEdit)

            val createCard = db.createCardForDeck(
                user.email,
                importedDeckId,
                ManageCardInput(
                    type = CardType.HISTORY_FACT,
                    frontTextPl = "Kto to?",
                    backRuTranslation = "Opis",
                    historyAnswerPl = "Opis",
                    tags = listOf("imported"),
                ),
            )
            assertEquals(MutationStatus.OK, createCard.status)
            val cardId = createCard.value!!.id

            assertEquals(MutationStatus.OK, db.deleteManagedCard(user.email, cardId))
        }
    }
}

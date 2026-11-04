package org.example.db

import com.zaxxer.hikari.HikariConfig
import com.zaxxer.hikari.HikariDataSource
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.example.config.AppConfig
import org.example.model.AudioQuality
import org.example.model.AudioSource
import org.example.model.AudioVariantDto
import org.example.model.CardDto
import org.example.model.CardType
import org.example.model.DeckDto
import org.example.model.ManageCardDto
import org.example.model.ManageDeckDto
import org.example.model.ReviewAnswerRequest
import org.example.model.ReviewAnswerResponse
import org.example.model.UserDto
import org.example.service.ReviewStateInput
import org.example.service.Sm2Scheduler
import java.io.Closeable
import java.nio.file.Files
import java.sql.Connection
import java.time.Instant
import java.util.UUID
import kotlin.io.path.createDirectories
import kotlin.io.path.exists

class AppDatabase(
    config: AppConfig,
    private val json: Json = Json,
) : Closeable {
    private val dataSource: HikariDataSource

    init {
        val dbParent = config.dbPath.parent
        if (dbParent != null && !dbParent.exists()) {
            dbParent.createDirectories()
        }

        if (!config.mediaRoot.exists()) {
            Files.createDirectories(config.mediaRoot)
        }

        val hikariConfig = HikariConfig().apply {
            jdbcUrl = "jdbc:sqlite:${config.dbPath}"
            maximumPoolSize = 6
            isAutoCommit = true
            connectionInitSql = "PRAGMA foreign_keys = ON;"
            validate()
        }
        dataSource = HikariDataSource(hikariConfig)

        connection().use { conn ->
            conn.createStatement().use { st ->
                st.execute("PRAGMA journal_mode = WAL")
                st.execute("PRAGMA foreign_keys = ON")
            }
        }

        bootstrap()
        migrateSchema()
        ensureDefaultDecks()
    }

    fun upsertUser(email: String, name: String, role: String): UserDto {
        connection().use { conn ->
            conn.prepareStatement(
                """
                INSERT INTO users(email, name, role)
                VALUES (?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    name = excluded.name,
                    role = excluded.role
                """.trimIndent()
            ).use { ps ->
                ps.setString(1, email.lowercase())
                ps.setString(2, name)
                ps.setString(3, role)
                ps.executeUpdate()
            }
        }

        return requireNotNull(getUser(email))
    }

    fun getUser(email: String): UserDto? {
        connection().use { conn ->
            conn.prepareStatement("SELECT email, name, role FROM users WHERE email = ?").use { ps ->
                ps.setString(1, email.lowercase())
                ps.executeQuery().use { rs ->
                    if (!rs.next()) {
                        return null
                    }

                    return UserDto(
                        email = rs.getString("email"),
                        name = rs.getString("name"),
                        role = rs.getString("role"),
                    )
                }
            }
        }
    }

    fun listDecksWithDue(email: String, now: Instant = Instant.now()): List<DeckDto> {
        val decks = mutableListOf<DeckDto>()

        connection().use { conn ->
            conn.prepareStatement(
                """
                SELECT
                    d.id,
                    d.code,
                    d.title,
                    COALESCE(SUM(CASE
                        WHEN c.is_active = 1
                         AND (rs.card_id IS NULL OR rs.due_at <= ?)
                        THEN 1
                        ELSE 0
                    END), 0) AS due_count
                FROM decks d
                LEFT JOIN cards c ON c.deck_id = d.id
                LEFT JOIN review_state rs
                  ON rs.card_id = c.id
                 AND rs.user_email = ?
                WHERE d.owner_email IS NULL OR d.owner_email = ?
                GROUP BY d.id, d.code, d.title
                ORDER BY d.id ASC
                """.trimIndent()
            ).use { ps ->
                ps.setString(1, now.toString())
                ps.setString(2, email.lowercase())
                ps.setString(3, email.lowercase())
                ps.executeQuery().use { rs ->
                    while (rs.next()) {
                        decks += DeckDto(
                            id = rs.getLong("id"),
                            code = rs.getString("code"),
                            title = rs.getString("title"),
                            dueCount = rs.getInt("due_count"),
                        )
                    }
                }
            }
        }

        return decks
    }

    fun resetDeckProgress(email: String, deckCode: String): MutationResult<Int> {
        val normalizedEmail = email.lowercase()
        val normalizedDeckCode = deckCode.trim()
        if (normalizedDeckCode.isBlank()) {
            return MutationResult(MutationStatus.INVALID)
        }

        connection().use { conn ->
            conn.autoCommit = false
            try {
                val deckMeta = conn.prepareStatement(
                    """
                    SELECT id, owner_email
                    FROM decks
                    WHERE code = ?
                    """.trimIndent()
                ).use { ps ->
                    ps.setString(1, normalizedDeckCode)
                    ps.executeQuery().use { rs ->
                        if (!rs.next()) {
                            null
                        } else {
                            rs.getLong("id") to rs.getString("owner_email")
                        }
                    }
                } ?: run {
                    conn.rollback()
                    return MutationResult(MutationStatus.NOT_FOUND)
                }

                val (deckId, ownerEmail) = deckMeta
                if (ownerEmail != null && !ownerEmail.equals(normalizedEmail, ignoreCase = true)) {
                    conn.rollback()
                    return MutationResult(MutationStatus.FORBIDDEN)
                }

                val deleted = conn.prepareStatement(
                    """
                    DELETE FROM review_state
                    WHERE user_email = ?
                      AND card_id IN (
                        SELECT id
                        FROM cards
                        WHERE deck_id = ? AND is_active = 1
                      )
                    """.trimIndent()
                ).use { ps ->
                    ps.setString(1, normalizedEmail)
                    ps.setLong(2, deckId)
                    ps.executeUpdate()
                }

                conn.commit()
                return MutationResult(MutationStatus.OK, deleted)
            } catch (ex: Exception) {
                conn.rollback()
                throw ex
            } finally {
                conn.autoCommit = true
            }
        }
    }

    fun getNextCards(email: String, deckCode: String?, limit: Int, now: Instant = Instant.now()): List<CardDto> {
        val cards = mutableListOf<CardDto>()

        val sql = buildString {
            append(
                """
                SELECT
                    c.id,
                    c.deck_id,
                    c.type,
                    c.front_text_pl,
                    c.front_transcript,
                    c.back_ru_translation,
                    c.back_ru_pron_hint,
                    c.date_iso,
                    c.image_path,
                    c.history_answer_pl,
                    c.history_answer_ru,
                    c.tags_json
                FROM cards c
                JOIN decks d ON d.id = c.deck_id
                LEFT JOIN review_state rs
                  ON rs.card_id = c.id
                 AND rs.user_email = ?
                WHERE c.is_active = 1
                  AND (rs.card_id IS NULL OR rs.due_at <= ?)
                  AND (d.owner_email IS NULL OR d.owner_email = ?)
                """.trimIndent()
            )
            if (!deckCode.isNullOrBlank()) {
                append(" AND d.code = ?")
            }
            append(
                """
                ORDER BY
                    CASE WHEN rs.due_at IS NULL THEN 0 ELSE 1 END ASC,
                    CASE WHEN rs.due_at IS NULL THEN RANDOM() ELSE 0 END ASC,
                    rs.due_at ASC,
                    c.id ASC
                LIMIT ?
                """.trimIndent()
            )
        }

        connection().use { conn ->
            conn.prepareStatement(sql).use { ps ->
                var idx = 1
                ps.setString(idx++, email.lowercase())
                ps.setString(idx++, now.toString())
                ps.setString(idx++, email.lowercase())
                if (!deckCode.isNullOrBlank()) {
                    ps.setString(idx++, deckCode)
                }
                ps.setInt(idx, limit)

                ps.executeQuery().use { rs ->
                    while (rs.next()) {
                        cards += CardDto(
                            id = rs.getLong("id"),
                            deckId = rs.getLong("deck_id"),
                            type = CardType.valueOf(rs.getString("type")),
                            frontTextPl = rs.getString("front_text_pl"),
                            frontTranscript = rs.getString("front_transcript"),
                            backRuTranslation = rs.getString("back_ru_translation"),
                            backRuPronHint = rs.getString("back_ru_pron_hint"),
                            dateIso = rs.getString("date_iso"),
                            imagePath = rs.getString("image_path"),
                            historyAnswerPl = rs.getString("history_answer_pl"),
                            historyAnswerRu = rs.getString("history_answer_ru"),
                            tags = decodeTags(rs.getString("tags_json")),
                            audioVariants = emptyList(),
                        )
                    }
                }
            }
        }

        if (cards.isEmpty()) {
            return cards
        }

        val audioByCard = loadAudioVariants(cards.map { it.id })
        return cards.map { it.copy(audioVariants = audioByCard[it.id].orEmpty()) }
    }

    fun getCard(cardId: Long): CardDto? = getCard(null, cardId)

    fun getCard(email: String?, cardId: Long): CardDto? {
        connection().use { conn ->
            conn.prepareStatement(
                """
                SELECT
                    c.id,
                    c.deck_id,
                    c.type,
                    c.front_text_pl,
                    c.front_transcript,
                    c.back_ru_translation,
                    c.back_ru_pron_hint,
                    c.date_iso,
                    c.image_path,
                    c.history_answer_pl,
                    c.history_answer_ru,
                    c.tags_json
                FROM cards c
                JOIN decks d ON d.id = c.deck_id
                WHERE c.id = ?
                  AND c.is_active = 1
                  AND (? IS NULL OR d.owner_email IS NULL OR d.owner_email = ?)
                """.trimIndent()
            ).use { ps ->
                ps.setLong(1, cardId)
                ps.setString(2, email?.lowercase())
                ps.setString(3, email?.lowercase())
                ps.executeQuery().use { rs ->
                    if (!rs.next()) {
                        return null
                    }

                    val card = CardDto(
                        id = rs.getLong("id"),
                        deckId = rs.getLong("deck_id"),
                        type = CardType.valueOf(rs.getString("type")),
                        frontTextPl = rs.getString("front_text_pl"),
                        frontTranscript = rs.getString("front_transcript"),
                        backRuTranslation = rs.getString("back_ru_translation"),
                        backRuPronHint = rs.getString("back_ru_pron_hint"),
                        dateIso = rs.getString("date_iso"),
                        imagePath = rs.getString("image_path"),
                        historyAnswerPl = rs.getString("history_answer_pl"),
                        historyAnswerRu = rs.getString("history_answer_ru"),
                        tags = decodeTags(rs.getString("tags_json")),
                    )

                    return card.copy(audioVariants = loadAudioVariants(listOf(card.id))[card.id].orEmpty())
                }
            }
        }
    }

    fun getCardAudio(cardId: Long): List<AudioVariantDto> {
        return loadAudioVariants(listOf(cardId))[cardId].orEmpty()
    }

    fun listManageDecks(email: String, now: Instant = Instant.now()): List<ManageDeckDto> {
        val normalizedEmail = email.lowercase()
        val decks = mutableListOf<ManageDeckDto>()

        connection().use { conn ->
            conn.prepareStatement(
                """
                SELECT
                    d.id,
                    d.code,
                    d.title,
                    d.owner_email,
                    d.is_system,
                    COALESCE(SUM(CASE
                        WHEN c.is_active = 1
                         AND (rs.card_id IS NULL OR rs.due_at <= ?)
                        THEN 1
                        ELSE 0
                    END), 0) AS due_count,
                    COALESCE(SUM(CASE
                        WHEN c.is_active = 1 THEN 1 ELSE 0
                    END), 0) AS card_count
                FROM decks d
                LEFT JOIN cards c ON c.deck_id = d.id
                LEFT JOIN review_state rs
                  ON rs.card_id = c.id
                 AND rs.user_email = ?
                WHERE d.owner_email IS NULL OR d.owner_email = ?
                GROUP BY d.id, d.code, d.title, d.owner_email, d.is_system
                ORDER BY d.is_system DESC, d.id ASC
                """.trimIndent()
            ).use { ps ->
                ps.setString(1, now.toString())
                ps.setString(2, normalizedEmail)
                ps.setString(3, normalizedEmail)
                ps.executeQuery().use { rs ->
                    while (rs.next()) {
                        val ownerEmail = rs.getString("owner_email")
                        val isSystem = rs.getInt("is_system") == 1
                        decks += ManageDeckDto(
                            id = rs.getLong("id"),
                            code = rs.getString("code"),
                            title = rs.getString("title"),
                            dueCount = rs.getInt("due_count"),
                            cardCount = rs.getInt("card_count"),
                            ownerEmail = ownerEmail,
                            isSystem = isSystem,
                            canEdit = canEditDeck(ownerEmail, isSystem, normalizedEmail),
                        )
                    }
                }
            }
        }

        return decks
    }

    fun createDeckForUser(email: String, code: String, title: String): MutationResult<ManageDeckDto> {
        val normalizedEmail = email.lowercase()
        val normalizedCode = normalizeDeckCode(code) ?: return MutationResult(MutationStatus.INVALID)
        val normalizedTitle = title.trim()
        if (normalizedTitle.isBlank()) {
            return MutationResult(MutationStatus.INVALID)
        }

        connection().use { conn ->
            try {
                conn.prepareStatement(
                    """
                    INSERT INTO decks(code, title, owner_email, is_system)
                    VALUES (?, ?, ?, 0)
                    """.trimIndent()
                ).use { ps ->
                    ps.setString(1, normalizedCode)
                    ps.setString(2, normalizedTitle)
                    ps.setString(3, normalizedEmail)
                    ps.executeUpdate()
                }
            } catch (ex: Exception) {
                if (isUniqueConstraint(ex)) {
                    return MutationResult(MutationStatus.CONFLICT)
                }
                throw ex
            }

            conn.prepareStatement("SELECT id FROM decks WHERE code = ?").use { ps ->
                ps.setString(1, normalizedCode)
                ps.executeQuery().use { rs ->
                    if (!rs.next()) {
                        return MutationResult(MutationStatus.NOT_FOUND)
                    }
                    val createdDeck = loadManageDeck(conn, normalizedEmail, rs.getLong("id"))
                        ?: return MutationResult(MutationStatus.NOT_FOUND)
                    return MutationResult(MutationStatus.OK, createdDeck)
                }
            }
        }
    }

    fun updateDeckForUser(email: String, deckId: Long, code: String, title: String): MutationResult<ManageDeckDto> {
        val normalizedEmail = email.lowercase()
        val normalizedCode = normalizeDeckCode(code) ?: return MutationResult(MutationStatus.INVALID)
        val normalizedTitle = title.trim()
        if (normalizedTitle.isBlank()) {
            return MutationResult(MutationStatus.INVALID)
        }

        connection().use { conn ->
            val access = loadDeckAccess(conn, normalizedEmail, deckId)
            if (access == DeckAccess.NOT_FOUND) {
                return MutationResult(MutationStatus.NOT_FOUND)
            }
            if (access != DeckAccess.EDITABLE) {
                return MutationResult(MutationStatus.FORBIDDEN)
            }

            try {
                conn.prepareStatement(
                    """
                    UPDATE decks
                    SET code = ?, title = ?
                    WHERE id = ?
                    """.trimIndent()
                ).use { ps ->
                    ps.setString(1, normalizedCode)
                    ps.setString(2, normalizedTitle)
                    ps.setLong(3, deckId)
                    ps.executeUpdate()
                }
            } catch (ex: Exception) {
                if (isUniqueConstraint(ex)) {
                    return MutationResult(MutationStatus.CONFLICT)
                }
                throw ex
            }

            val updatedDeck = loadManageDeck(conn, normalizedEmail, deckId)
                ?: return MutationResult(MutationStatus.NOT_FOUND)
            return MutationResult(MutationStatus.OK, updatedDeck)
        }
    }

    fun deleteDeckForUser(email: String, deckId: Long): MutationStatus {
        val normalizedEmail = email.lowercase()

        connection().use { conn ->
            conn.autoCommit = false
            try {
                val access = loadDeckAccess(conn, normalizedEmail, deckId)
                if (access == DeckAccess.NOT_FOUND) {
                    conn.rollback()
                    return MutationStatus.NOT_FOUND
                }
                if (access != DeckAccess.EDITABLE) {
                    conn.rollback()
                    return MutationStatus.FORBIDDEN
                }

                conn.prepareStatement(
                    "DELETE FROM review_events WHERE card_id IN (SELECT id FROM cards WHERE deck_id = ?)"
                ).use { ps ->
                    ps.setLong(1, deckId)
                    ps.executeUpdate()
                }

                conn.prepareStatement("DELETE FROM cards WHERE deck_id = ?").use { ps ->
                    ps.setLong(1, deckId)
                    ps.executeUpdate()
                }

                conn.prepareStatement("DELETE FROM decks WHERE id = ?").use { ps ->
                    ps.setLong(1, deckId)
                    ps.executeUpdate()
                }

                conn.commit()
                return MutationStatus.OK
            } catch (ex: Exception) {
                conn.rollback()
                throw ex
            } finally {
                conn.autoCommit = true
            }
        }
    }

    fun listDeckCardsForManage(email: String, deckId: Long): MutationResult<List<ManageCardDto>> {
        val normalizedEmail = email.lowercase()
        val cards = mutableListOf<ManageCardDto>()

        connection().use { conn ->
            val access = loadDeckAccess(conn, normalizedEmail, deckId)
            if (access == DeckAccess.NOT_FOUND) {
                return MutationResult(MutationStatus.NOT_FOUND)
            }
            if (access == DeckAccess.FORBIDDEN) {
                return MutationResult(MutationStatus.FORBIDDEN)
            }

            conn.prepareStatement(
                """
                SELECT
                    id,
                    deck_id,
                    type,
                    front_text_pl,
                    front_transcript,
                    back_ru_translation,
                    back_ru_pron_hint,
                    date_iso,
                    image_path,
                    history_answer_pl,
                    history_answer_ru,
                    tags_json
                FROM cards
                WHERE deck_id = ? AND is_active = 1
                ORDER BY id DESC
                """.trimIndent()
            ).use { ps ->
                ps.setLong(1, deckId)
                ps.executeQuery().use { rs ->
                    while (rs.next()) {
                        cards += ManageCardDto(
                            id = rs.getLong("id"),
                            deckId = rs.getLong("deck_id"),
                            type = CardType.valueOf(rs.getString("type")),
                            frontTextPl = rs.getString("front_text_pl"),
                            frontTranscript = rs.getString("front_transcript"),
                            backRuTranslation = rs.getString("back_ru_translation"),
                            backRuPronHint = rs.getString("back_ru_pron_hint"),
                            dateIso = rs.getString("date_iso"),
                            imagePath = rs.getString("image_path"),
                            historyAnswerPl = rs.getString("history_answer_pl"),
                            historyAnswerRu = rs.getString("history_answer_ru"),
                            tags = decodeTags(rs.getString("tags_json")),
                        )
                    }
                }
            }
        }

        return MutationResult(MutationStatus.OK, cards)
    }

    fun createCardForDeck(email: String, deckId: Long, input: ManageCardInput): MutationResult<ManageCardDto> {
        val normalizedEmail = email.lowercase()
        val sanitized = sanitizeManageCardInput(input) ?: return MutationResult(MutationStatus.INVALID)

        connection().use { conn ->
            conn.autoCommit = false
            try {
                val access = loadDeckAccess(conn, normalizedEmail, deckId)
                if (access == DeckAccess.NOT_FOUND) {
                    conn.rollback()
                    return MutationResult(MutationStatus.NOT_FOUND)
                }
                if (access != DeckAccess.EDITABLE) {
                    conn.rollback()
                    return MutationResult(MutationStatus.FORBIDDEN)
                }

                val canonicalKey = "MANUAL|$deckId|${UUID.randomUUID()}"
                conn.prepareStatement(
                    """
                    INSERT INTO cards(
                        deck_id,
                        type,
                        canonical_key,
                        front_text_pl,
                        front_transcript,
                        back_ru_translation,
                        back_ru_pron_hint,
                        date_iso,
                        image_path,
                        history_answer_pl,
                        history_answer_ru,
                        tags_json,
                        is_active,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                    """.trimIndent()
                ).use { ps ->
                    ps.setLong(1, deckId)
                    ps.setString(2, sanitized.type.name)
                    ps.setString(3, canonicalKey)
                    ps.setString(4, sanitized.frontTextPl)
                    ps.setString(5, sanitized.frontTranscript)
                    ps.setString(6, sanitized.backRuTranslation)
                    ps.setString(7, sanitized.backRuPronHint)
                    ps.setString(8, sanitized.dateIso)
                    ps.setString(9, sanitized.imagePath)
                    ps.setString(10, sanitized.historyAnswerPl)
                    ps.setString(11, sanitized.historyAnswerRu)
                    ps.setString(12, json.encodeToString(sanitized.tags))
                    ps.executeUpdate()
                }

                val cardId = conn.prepareStatement("SELECT last_insert_rowid()").use { ps ->
                    ps.executeQuery().use { rs -> if (rs.next()) rs.getLong(1) else 0L }
                }
                if (cardId == 0L) {
                    conn.rollback()
                    return MutationResult(MutationStatus.NOT_FOUND)
                }

                val created = loadManageCard(conn, cardId)
                    ?: run {
                        conn.rollback()
                        return MutationResult(MutationStatus.NOT_FOUND)
                    }

                conn.commit()
                return MutationResult(MutationStatus.OK, created)
            } catch (ex: Exception) {
                conn.rollback()
                throw ex
            } finally {
                conn.autoCommit = true
            }
        }
    }

    fun updateManagedCard(email: String, cardId: Long, input: ManageCardInput): MutationResult<ManageCardDto> {
        val normalizedEmail = email.lowercase()
        val sanitized = sanitizeManageCardInput(input) ?: return MutationResult(MutationStatus.INVALID)

        connection().use { conn ->
            conn.autoCommit = false
            try {
                val access = loadCardAccess(conn, normalizedEmail, cardId)
                if (access == DeckAccess.NOT_FOUND) {
                    conn.rollback()
                    return MutationResult(MutationStatus.NOT_FOUND)
                }
                if (access != DeckAccess.EDITABLE) {
                    conn.rollback()
                    return MutationResult(MutationStatus.FORBIDDEN)
                }

                val updated = conn.prepareStatement(
                    """
                    UPDATE cards
                    SET
                        type = ?,
                        front_text_pl = ?,
                        front_transcript = ?,
                        back_ru_translation = ?,
                        back_ru_pron_hint = ?,
                        date_iso = ?,
                        image_path = ?,
                        history_answer_pl = ?,
                        history_answer_ru = ?,
                        tags_json = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND is_active = 1
                    """.trimIndent()
                ).use { ps ->
                    ps.setString(1, sanitized.type.name)
                    ps.setString(2, sanitized.frontTextPl)
                    ps.setString(3, sanitized.frontTranscript)
                    ps.setString(4, sanitized.backRuTranslation)
                    ps.setString(5, sanitized.backRuPronHint)
                    ps.setString(6, sanitized.dateIso)
                    ps.setString(7, sanitized.imagePath)
                    ps.setString(8, sanitized.historyAnswerPl)
                    ps.setString(9, sanitized.historyAnswerRu)
                    ps.setString(10, json.encodeToString(sanitized.tags))
                    ps.setLong(11, cardId)
                    ps.executeUpdate()
                }
                if (updated == 0) {
                    conn.rollback()
                    return MutationResult(MutationStatus.NOT_FOUND)
                }

                val card = loadManageCard(conn, cardId)
                    ?: run {
                        conn.rollback()
                        return MutationResult(MutationStatus.NOT_FOUND)
                    }

                conn.commit()
                return MutationResult(MutationStatus.OK, card)
            } catch (ex: Exception) {
                conn.rollback()
                throw ex
            } finally {
                conn.autoCommit = true
            }
        }
    }

    fun deleteManagedCard(email: String, cardId: Long): MutationStatus {
        val normalizedEmail = email.lowercase()

        connection().use { conn ->
            conn.autoCommit = false
            try {
                val access = loadCardAccess(conn, normalizedEmail, cardId)
                if (access == DeckAccess.NOT_FOUND) {
                    conn.rollback()
                    return MutationStatus.NOT_FOUND
                }
                if (access != DeckAccess.EDITABLE) {
                    conn.rollback()
                    return MutationStatus.FORBIDDEN
                }

                conn.prepareStatement("DELETE FROM review_events WHERE card_id = ?").use { ps ->
                    ps.setLong(1, cardId)
                    ps.executeUpdate()
                }
                conn.prepareStatement("DELETE FROM cards WHERE id = ?").use { ps ->
                    ps.setLong(1, cardId)
                    ps.executeUpdate()
                }

                conn.commit()
                return MutationStatus.OK
            } catch (ex: Exception) {
                conn.rollback()
                throw ex
            } finally {
                conn.autoCommit = true
            }
        }
    }

    fun applyReview(
        email: String,
        request: ReviewAnswerRequest,
        scheduler: Sm2Scheduler,
        now: Instant = Instant.now(),
    ): ReviewAnswerResponse {
        connection().use { conn ->
            conn.autoCommit = false
            try {
                val current = loadReviewState(conn, email, request.cardId)
                val scheduled = scheduler.schedule(current, request.grade, now)

                conn.prepareStatement(
                    """
                    INSERT INTO review_state(
                        user_email,
                        card_id,
                        ease_factor,
                        interval_days,
                        repetition,
                        lapse_count,
                        due_at,
                        last_reviewed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_email, card_id) DO UPDATE SET
                        ease_factor = excluded.ease_factor,
                        interval_days = excluded.interval_days,
                        repetition = excluded.repetition,
                        lapse_count = excluded.lapse_count,
                        due_at = excluded.due_at,
                        last_reviewed_at = excluded.last_reviewed_at
                    """.trimIndent()
                ).use { ps ->
                    ps.setString(1, email.lowercase())
                    ps.setLong(2, request.cardId)
                    ps.setDouble(3, scheduled.easeFactor)
                    ps.setInt(4, scheduled.intervalDays)
                    ps.setInt(5, scheduled.repetition)
                    ps.setInt(6, scheduled.lapseCount)
                    ps.setString(7, scheduled.dueAt.toString())
                    ps.setString(8, now.toString())
                    ps.executeUpdate()
                }

                conn.prepareStatement(
                    """
                    INSERT INTO review_events(
                        user_email,
                        card_id,
                        grade,
                        response_ms,
                        reviewed_at,
                        prev_interval_days,
                        new_interval_days,
                        prev_ease,
                        new_ease
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """.trimIndent()
                ).use { ps ->
                    ps.setString(1, email.lowercase())
                    ps.setLong(2, request.cardId)
                    ps.setString(3, request.grade.name)
                    ps.setInt(4, request.responseMs ?: 0)
                    ps.setString(5, now.toString())
                    ps.setInt(6, current.intervalDays)
                    ps.setInt(7, scheduled.intervalDays)
                    ps.setDouble(8, current.easeFactor)
                    ps.setDouble(9, scheduled.easeFactor)
                    ps.executeUpdate()
                }

                conn.commit()

                return ReviewAnswerResponse(
                    cardId = request.cardId,
                    dueAt = scheduled.dueAt.toString(),
                    intervalDays = scheduled.intervalDays,
                    easeFactor = scheduled.easeFactor,
                    repetition = scheduled.repetition,
                    lapseCount = scheduled.lapseCount,
                )
            } catch (ex: Exception) {
                conn.rollback()
                throw ex
            } finally {
                conn.autoCommit = true
            }
        }
    }

    fun upsertDeck(code: String, title: String, ownerEmail: String? = null, isSystem: Boolean = false): Long {
        connection().use { conn ->
            conn.prepareStatement(
                """
                INSERT INTO decks(code, title, owner_email, is_system)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    title = excluded.title
                """.trimIndent()
            ).use { ps ->
                ps.setString(1, code)
                ps.setString(2, title)
                ps.setString(3, ownerEmail?.lowercase())
                ps.setInt(4, if (isSystem) 1 else 0)
                ps.executeUpdate()
            }

            conn.prepareStatement("SELECT id FROM decks WHERE code = ?").use { ps ->
                ps.setString(1, code)
                ps.executeQuery().use { rs ->
                    if (rs.next()) {
                        return rs.getLong(1)
                    }
                }
            }
        }

        error("Failed to upsert deck: $code")
    }

    fun upsertCard(input: UpsertCardInput): Long {
        connection().use { conn ->
            conn.prepareStatement(
                """
                INSERT INTO cards(
                    deck_id,
                    type,
                    canonical_key,
                    front_text_pl,
                    front_transcript,
                    back_ru_translation,
                    back_ru_pron_hint,
                    date_iso,
                    image_path,
                    history_answer_pl,
                    history_answer_ru,
                    tags_json,
                    is_active,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
                ON CONFLICT(canonical_key) DO UPDATE SET
                    deck_id = excluded.deck_id,
                    type = excluded.type,
                    front_text_pl = excluded.front_text_pl,
                    front_transcript = excluded.front_transcript,
                    back_ru_translation = excluded.back_ru_translation,
                    back_ru_pron_hint = excluded.back_ru_pron_hint,
                    date_iso = excluded.date_iso,
                    image_path = excluded.image_path,
                    history_answer_pl = excluded.history_answer_pl,
                    history_answer_ru = excluded.history_answer_ru,
                    tags_json = excluded.tags_json,
                    is_active = 1,
                    updated_at = CURRENT_TIMESTAMP
                """.trimIndent()
            ).use { ps ->
                ps.setLong(1, input.deckId)
                ps.setString(2, input.type.name)
                ps.setString(3, input.canonicalKey)
                ps.setString(4, input.frontTextPl)
                ps.setString(5, input.frontTranscript)
                ps.setString(6, input.backRuTranslation)
                ps.setString(7, input.backRuPronHint)
                ps.setString(8, input.dateIso)
                ps.setString(9, input.imagePath)
                ps.setString(10, input.historyAnswerPl)
                ps.setString(11, input.historyAnswerRu)
                ps.setString(12, json.encodeToString(input.tags))
                ps.executeUpdate()
            }

            conn.prepareStatement("SELECT id FROM cards WHERE canonical_key = ?").use { ps ->
                ps.setString(1, input.canonicalKey)
                ps.executeQuery().use { rs ->
                    if (rs.next()) {
                        return rs.getLong(1)
                    }
                }
            }
        }

        error("Failed to upsert card: ${input.canonicalKey}")
    }

    fun upsertAudio(cardId: Long, input: UpsertAudioInput) {
        connection().use { conn ->
            conn.autoCommit = false
            try {
                if (input.isPrimary) {
                    conn.prepareStatement("UPDATE audio_variants SET is_primary = 0 WHERE card_id = ?").use { ps ->
                        ps.setLong(1, cardId)
                        ps.executeUpdate()
                    }
                }

                conn.prepareStatement(
                    """
                    INSERT INTO audio_variants(
                        card_id,
                        source,
                        label,
                        file_path,
                        quality,
                        is_primary,
                        license_text,
                        duration_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(card_id, source, file_path) DO UPDATE SET
                        label = excluded.label,
                        quality = excluded.quality,
                        is_primary = excluded.is_primary,
                        license_text = excluded.license_text,
                        duration_ms = excluded.duration_ms
                    """.trimIndent()
                ).use { ps ->
                    ps.setLong(1, cardId)
                    ps.setString(2, input.source.name)
                    ps.setString(3, input.label.ifBlank { defaultAudioLabel(input.source) })
                    ps.setString(4, input.path)
                    ps.setString(5, input.quality.name)
                    ps.setInt(6, if (input.isPrimary) 1 else 0)
                    ps.setString(7, input.license)
                    if (input.durationMs != null) {
                        ps.setInt(8, input.durationMs)
                    } else {
                        ps.setNull(8, java.sql.Types.INTEGER)
                    }
                    ps.executeUpdate()
                }

                conn.commit()
            } catch (ex: Exception) {
                conn.rollback()
                throw ex
            } finally {
                conn.autoCommit = true
            }
        }
    }

    fun upsertAttribution(cardId: Long, input: UpsertAttributionInput) {
        connection().use { conn ->
            conn.prepareStatement(
                """
                INSERT INTO source_attribution(
                    card_id,
                    source_name,
                    source_ref,
                    confidence_score,
                    license_text
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(card_id, source_name, source_ref) DO UPDATE SET
                    confidence_score = excluded.confidence_score,
                    license_text = excluded.license_text
                """.trimIndent()
            ).use { ps ->
                ps.setLong(1, cardId)
                ps.setString(2, input.sourceName)
                ps.setString(3, input.sourceRef)
                ps.setDouble(4, input.confidenceScore)
                ps.setString(5, input.licenseText)
                ps.executeUpdate()
            }
        }
    }

    private fun loadReviewState(conn: Connection, email: String, cardId: Long): ReviewStateInput {
        conn.prepareStatement(
            """
            SELECT ease_factor, interval_days, repetition, lapse_count
            FROM review_state
            WHERE user_email = ? AND card_id = ?
            """.trimIndent()
        ).use { ps ->
            ps.setString(1, email.lowercase())
            ps.setLong(2, cardId)
            ps.executeQuery().use { rs ->
                if (!rs.next()) {
                    return ReviewStateInput()
                }

                return ReviewStateInput(
                    easeFactor = rs.getDouble("ease_factor"),
                    intervalDays = rs.getInt("interval_days"),
                    repetition = rs.getInt("repetition"),
                    lapseCount = rs.getInt("lapse_count"),
                )
            }
        }
    }

    private fun loadAudioVariants(cardIds: List<Long>): Map<Long, List<AudioVariantDto>> {
        if (cardIds.isEmpty()) {
            return emptyMap()
        }

        val sql = buildString {
            append("SELECT id, card_id, source, label, file_path, quality, is_primary, license_text, duration_ms FROM audio_variants WHERE card_id IN (")
            append(cardIds.joinToString(",") { "?" })
            append(")")
        }

        val variants = mutableListOf<AudioVariantDto>()
        connection().use { conn ->
            conn.prepareStatement(sql).use { ps ->
                cardIds.forEachIndexed { idx, id -> ps.setLong(idx + 1, id) }
                ps.executeQuery().use { rs ->
                    while (rs.next()) {
                        val source = AudioSource.valueOf(rs.getString("source"))
                        variants += AudioVariantDto(
                            id = rs.getLong("id"),
                            cardId = rs.getLong("card_id"),
                            source = source,
                            label = rs.getString("label")?.ifBlank { defaultAudioLabel(source) } ?: defaultAudioLabel(source),
                            path = rs.getString("file_path"),
                            quality = AudioQuality.valueOf(rs.getString("quality")),
                            isPrimary = rs.getInt("is_primary") == 1,
                            license = rs.getString("license_text"),
                            durationMs = rs.getObject("duration_ms") as? Int,
                        )
                    }
                }
            }
        }

        return variants
            .groupBy { it.cardId }
            .mapValues { (_, group) ->
                group.sortedBy { audioSortPriority(it) }
            }
    }

    private fun audioSortPriority(audio: AudioVariantDto): Int {
        val isNative = audio.source == AudioSource.NATIVE_OPUS || audio.source == AudioSource.NATIVE_TATOEBA || audio.source == AudioSource.USER_RECORDING
        val isTts = audio.source == AudioSource.TTS_GOOGLE || audio.source == AudioSource.TTS_AZURE || audio.source == AudioSource.TTS_OPENAI

        return when {
            isNative && audio.quality == AudioQuality.VERIFIED -> 0
            isNative && audio.quality == AudioQuality.AUTO -> 1
            isTts && audio.isPrimary -> 2
            isTts -> 3
            else -> 4
        }
    }

    private fun decodeTags(tagsJson: String?): List<String> {
        if (tagsJson.isNullOrBlank()) {
            return emptyList()
        }

        return try {
            json.decodeFromString<List<String>>(tagsJson)
        } catch (_: Exception) {
            emptyList()
        }
    }

    private fun migrateSchema() {
        connection().use { conn ->
            if (!columnExists(conn, "decks", "owner_email")) {
                conn.createStatement().use { st ->
                    st.execute("ALTER TABLE decks ADD COLUMN owner_email TEXT")
                }
            }
            if (!columnExists(conn, "decks", "is_system")) {
                conn.createStatement().use { st ->
                    st.execute("ALTER TABLE decks ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0")
                }
            }
            if (!columnExists(conn, "cards", "history_answer_pl")) {
                conn.createStatement().use { st ->
                    st.execute("ALTER TABLE cards ADD COLUMN history_answer_pl TEXT")
                }
            }
            if (!columnExists(conn, "cards", "history_answer_ru")) {
                conn.createStatement().use { st ->
                    st.execute("ALTER TABLE cards ADD COLUMN history_answer_ru TEXT")
                }
            }
        }
    }

    private fun columnExists(conn: Connection, table: String, column: String): Boolean {
        conn.prepareStatement("PRAGMA table_info($table)").use { ps ->
            ps.executeQuery().use { rs ->
                while (rs.next()) {
                    if (rs.getString("name").equals(column, ignoreCase = true)) {
                        return true
                    }
                }
            }
        }
        return false
    }

    private fun loadDeckAccess(conn: Connection, email: String, deckId: Long): DeckAccess {
        conn.prepareStatement(
            """
            SELECT owner_email, is_system
            FROM decks
            WHERE id = ?
            """.trimIndent()
        ).use { ps ->
            ps.setLong(1, deckId)
            ps.executeQuery().use { rs ->
                if (!rs.next()) {
                    return DeckAccess.NOT_FOUND
                }

                val ownerEmail = rs.getString("owner_email")
                val isSystem = rs.getInt("is_system") == 1
                if (ownerEmail != null && !ownerEmail.equals(email, ignoreCase = true)) {
                    return DeckAccess.FORBIDDEN
                }
                if (!canEditDeck(ownerEmail, isSystem, email)) {
                    return DeckAccess.READ_ONLY
                }
                return DeckAccess.EDITABLE
            }
        }
    }

    private fun loadCardAccess(conn: Connection, email: String, cardId: Long): DeckAccess {
        conn.prepareStatement(
            """
            SELECT d.owner_email, d.is_system
            FROM cards c
            JOIN decks d ON d.id = c.deck_id
            WHERE c.id = ? AND c.is_active = 1
            """.trimIndent()
        ).use { ps ->
            ps.setLong(1, cardId)
            ps.executeQuery().use { rs ->
                if (!rs.next()) {
                    return DeckAccess.NOT_FOUND
                }
                val ownerEmail = rs.getString("owner_email")
                val isSystem = rs.getInt("is_system") == 1
                if (ownerEmail != null && !ownerEmail.equals(email, ignoreCase = true)) {
                    return DeckAccess.FORBIDDEN
                }
                if (!canEditDeck(ownerEmail, isSystem, email)) {
                    return DeckAccess.READ_ONLY
                }
                return DeckAccess.EDITABLE
            }
        }
    }

    private fun loadManageDeck(conn: Connection, email: String, deckId: Long, now: Instant = Instant.now()): ManageDeckDto? {
        conn.prepareStatement(
            """
            SELECT
                d.id,
                d.code,
                d.title,
                d.owner_email,
                d.is_system,
                COALESCE(SUM(CASE
                    WHEN c.is_active = 1
                     AND (rs.card_id IS NULL OR rs.due_at <= ?)
                    THEN 1
                    ELSE 0
                END), 0) AS due_count,
                COALESCE(SUM(CASE WHEN c.is_active = 1 THEN 1 ELSE 0 END), 0) AS card_count
            FROM decks d
            LEFT JOIN cards c ON c.deck_id = d.id
            LEFT JOIN review_state rs
              ON rs.card_id = c.id
             AND rs.user_email = ?
            WHERE d.id = ?
              AND (d.owner_email IS NULL OR d.owner_email = ?)
            GROUP BY d.id, d.code, d.title, d.owner_email, d.is_system
            """.trimIndent()
        ).use { ps ->
            ps.setString(1, now.toString())
            ps.setString(2, email)
            ps.setLong(3, deckId)
            ps.setString(4, email)
            ps.executeQuery().use { rs ->
                if (!rs.next()) {
                    return null
                }

                val ownerEmail = rs.getString("owner_email")
                val isSystem = rs.getInt("is_system") == 1
                return ManageDeckDto(
                    id = rs.getLong("id"),
                    code = rs.getString("code"),
                    title = rs.getString("title"),
                    dueCount = rs.getInt("due_count"),
                    cardCount = rs.getInt("card_count"),
                    ownerEmail = ownerEmail,
                    isSystem = isSystem,
                    canEdit = canEditDeck(ownerEmail, isSystem, email),
                )
            }
        }
    }

    private fun loadManageCard(conn: Connection, cardId: Long): ManageCardDto? {
        conn.prepareStatement(
            """
            SELECT
                id,
                deck_id,
                type,
                front_text_pl,
                front_transcript,
                back_ru_translation,
                back_ru_pron_hint,
                date_iso,
                image_path,
                history_answer_pl,
                history_answer_ru,
                tags_json
            FROM cards
            WHERE id = ? AND is_active = 1
            """.trimIndent()
        ).use { ps ->
            ps.setLong(1, cardId)
            ps.executeQuery().use { rs ->
                if (!rs.next()) {
                    return null
                }

                return ManageCardDto(
                    id = rs.getLong("id"),
                    deckId = rs.getLong("deck_id"),
                    type = CardType.valueOf(rs.getString("type")),
                    frontTextPl = rs.getString("front_text_pl"),
                    frontTranscript = rs.getString("front_transcript"),
                    backRuTranslation = rs.getString("back_ru_translation"),
                    backRuPronHint = rs.getString("back_ru_pron_hint"),
                    dateIso = rs.getString("date_iso"),
                    imagePath = rs.getString("image_path"),
                    historyAnswerPl = rs.getString("history_answer_pl"),
                    historyAnswerRu = rs.getString("history_answer_ru"),
                    tags = decodeTags(rs.getString("tags_json")),
                )
            }
        }
    }

    private fun canEditDeck(ownerEmail: String?, isSystem: Boolean, email: String): Boolean {
        if (isSystem) {
            return false
        }
        if (ownerEmail == null) {
            // Imported non-system decks are shared and editable.
            return true
        }
        return ownerEmail.equals(email, ignoreCase = true)
    }

    private fun normalizeDeckCode(raw: String): String? {
        val code = raw.trim().lowercase()
        if (code.isBlank()) {
            return null
        }
        return if (code.matches(Regex("[a-z0-9][a-z0-9-]{1,63}"))) code else null
    }

    private fun sanitizeManageCardInput(input: ManageCardInput): ManageCardInput? {
        val front = input.frontTextPl.trim()
        val back = input.backRuTranslation.trim()
        if (front.isBlank()) {
            return null
        }

        val historyAnswerPl = input.historyAnswerPl.normalizeOptional()
        val historyAnswerRu = input.historyAnswerRu.normalizeOptional()
        val normalizedBack =
            if (input.type == CardType.HISTORY_FACT) {
                val fallback = historyAnswerRu ?: back.takeIf { it.isNotBlank() } ?: historyAnswerPl
                fallback ?: return null
            } else {
                back.takeIf { it.isNotBlank() } ?: return null
            }

        val tags = input.tags
            .asSequence()
            .map { it.trim() }
            .filter { it.isNotBlank() }
            .distinct()
            .take(32)
            .toList()

        return input.copy(
            frontTextPl = front,
            frontTranscript = input.frontTranscript.normalizeOptional(),
            backRuTranslation = normalizedBack,
            backRuPronHint = input.backRuPronHint.normalizeOptional(),
            dateIso = input.dateIso.normalizeOptional(),
            imagePath = input.imagePath.normalizeOptional(),
            historyAnswerPl = historyAnswerPl,
            historyAnswerRu = historyAnswerRu,
            tags = tags,
        )
    }

    private fun String?.normalizeOptional(): String? {
        val value = this?.trim()
        return if (value.isNullOrBlank()) null else value
    }

    private fun isUniqueConstraint(ex: Throwable): Boolean {
        val message = ex.message?.lowercase().orEmpty()
        return "unique constraint" in message || "constraint failed" in message
    }

    private fun ensureDefaultDecks() {
        upsertDeck("polish-core", "Polish Core", ownerEmail = null, isSystem = true)
        upsertDeck("history", "History Facts", ownerEmail = null, isSystem = true)
        connection().use { conn ->
            conn.prepareStatement(
                """
                UPDATE decks
                SET owner_email = NULL, is_system = 1
                WHERE code IN ('polish-core', 'history')
                """.trimIndent()
            ).use { it.executeUpdate() }
        }
    }

    private fun bootstrap() {
        connection().use { conn ->
            conn.createStatement().use { st ->
                st.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        email TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        role TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """.trimIndent()
                )

                st.execute(
                    """
                    CREATE TABLE IF NOT EXISTS decks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT NOT NULL UNIQUE,
                        title TEXT NOT NULL,
                        owner_email TEXT,
                        is_system INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """.trimIndent()
                )

                st.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cards (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        deck_id INTEGER NOT NULL REFERENCES decks(id),
                        type TEXT NOT NULL,
                        canonical_key TEXT NOT NULL UNIQUE,
                        front_text_pl TEXT NOT NULL,
                        front_transcript TEXT,
                        back_ru_translation TEXT NOT NULL,
                        back_ru_pron_hint TEXT,
                        date_iso TEXT,
                        image_path TEXT,
                        history_answer_pl TEXT,
                        history_answer_ru TEXT,
                        tags_json TEXT NOT NULL DEFAULT '[]',
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """.trimIndent()
                )

                st.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audio_variants (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                        source TEXT NOT NULL,
                        label TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        quality TEXT NOT NULL,
                        is_primary INTEGER NOT NULL DEFAULT 0,
                        license_text TEXT,
                        duration_ms INTEGER,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(card_id, source, file_path)
                    )
                    """.trimIndent()
                )

                st.execute(
                    """
                    CREATE TABLE IF NOT EXISTS review_state (
                        user_email TEXT NOT NULL REFERENCES users(email),
                        card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                        ease_factor REAL NOT NULL,
                        interval_days INTEGER NOT NULL,
                        repetition INTEGER NOT NULL,
                        lapse_count INTEGER NOT NULL,
                        due_at TEXT NOT NULL,
                        last_reviewed_at TEXT,
                        PRIMARY KEY(user_email, card_id)
                    )
                    """.trimIndent()
                )

                st.execute(
                    """
                    CREATE TABLE IF NOT EXISTS review_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_email TEXT NOT NULL,
                        card_id INTEGER NOT NULL,
                        grade TEXT NOT NULL,
                        response_ms INTEGER NOT NULL,
                        reviewed_at TEXT NOT NULL,
                        prev_interval_days INTEGER NOT NULL,
                        new_interval_days INTEGER NOT NULL,
                        prev_ease REAL NOT NULL,
                        new_ease REAL NOT NULL
                    )
                    """.trimIndent()
                )

                st.execute(
                    """
                    CREATE TABLE IF NOT EXISTS source_attribution (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                        source_name TEXT NOT NULL,
                        source_ref TEXT NOT NULL,
                        confidence_score REAL NOT NULL,
                        license_text TEXT,
                        UNIQUE(card_id, source_name, source_ref)
                    )
                    """.trimIndent()
                )
            }
        }
    }

    private fun defaultAudioLabel(source: AudioSource): String {
        return when (source) {
            AudioSource.TTS_GOOGLE -> "TTS (Google)"
            AudioSource.TTS_AZURE -> "TTS (Azure)"
            AudioSource.TTS_OPENAI -> "TTS (OpenAI)"
            AudioSource.NATIVE_TATOEBA -> "Native (Tatoeba)"
            AudioSource.NATIVE_OPUS -> "Native (OPUS)"
            AudioSource.USER_RECORDING -> "User Recording"
        }
    }

    private fun connection(): Connection = dataSource.connection

    override fun close() {
        dataSource.close()
    }
}

data class UpsertCardInput(
    val deckId: Long,
    val type: CardType,
    val canonicalKey: String,
    val frontTextPl: String,
    val frontTranscript: String? = null,
    val backRuTranslation: String,
    val backRuPronHint: String? = null,
    val dateIso: String? = null,
    val imagePath: String? = null,
    val historyAnswerPl: String? = null,
    val historyAnswerRu: String? = null,
    val tags: List<String> = emptyList(),
)

data class UpsertAudioInput(
    val source: AudioSource,
    val label: String,
    val path: String,
    val quality: AudioQuality,
    val isPrimary: Boolean,
    val license: String? = null,
    val durationMs: Int? = null,
)

data class UpsertAttributionInput(
    val sourceName: String,
    val sourceRef: String,
    val confidenceScore: Double,
    val licenseText: String? = null,
)

data class ManageCardInput(
    val type: CardType,
    val frontTextPl: String,
    val frontTranscript: String? = null,
    val backRuTranslation: String,
    val backRuPronHint: String? = null,
    val dateIso: String? = null,
    val imagePath: String? = null,
    val historyAnswerPl: String? = null,
    val historyAnswerRu: String? = null,
    val tags: List<String> = emptyList(),
)

enum class MutationStatus {
    OK,
    NOT_FOUND,
    FORBIDDEN,
    CONFLICT,
    INVALID,
}

data class MutationResult<T>(
    val status: MutationStatus,
    val value: T? = null,
)

private enum class DeckAccess {
    NOT_FOUND,
    FORBIDDEN,
    READ_ONLY,
    EDITABLE,
}

package org.example.model

import kotlinx.serialization.Serializable

@Serializable
enum class CardType {
    WORD,
    PHRASE,
    HISTORY_FACT,
}

@Serializable
enum class ReviewGrade {
    AGAIN,
    HARD,
    GOOD,
    EASY,
}

@Serializable
enum class AudioSource {
    TTS_GOOGLE,
    TTS_AZURE,
    TTS_OPENAI,
    NATIVE_TATOEBA,
    NATIVE_OPUS,
    USER_RECORDING,
}

@Serializable
enum class AudioQuality {
    AUTO,
    VERIFIED,
    REJECTED,
}

@Serializable
data class UserDto(
    val email: String,
    val name: String,
    val role: String,
)

@Serializable
data class DeckDto(
    val id: Long,
    val code: String,
    val title: String,
    val dueCount: Int,
)

@Serializable
data class ManageDeckDto(
    val id: Long,
    val code: String,
    val title: String,
    val dueCount: Int,
    val cardCount: Int,
    val ownerEmail: String? = null,
    val isSystem: Boolean = false,
    val canEdit: Boolean = false,
)

@Serializable
data class AudioVariantDto(
    val id: Long,
    val cardId: Long,
    val source: AudioSource,
    val label: String,
    val path: String,
    val quality: AudioQuality,
    val isPrimary: Boolean,
    val license: String? = null,
    val durationMs: Int? = null,
)

@Serializable
data class CardDto(
    val id: Long,
    val deckId: Long,
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
    val audioVariants: List<AudioVariantDto> = emptyList(),
)

@Serializable
data class ManageCardDto(
    val id: Long,
    val deckId: Long,
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

@Serializable
data class ReviewQueueResponse(
    val cards: List<CardDto>,
)

@Serializable
data class ReviewAnswerRequest(
    val cardId: Long,
    val grade: ReviewGrade,
    val responseMs: Int? = null,
)

@Serializable
data class ReviewAnswerResponse(
    val cardId: Long,
    val dueAt: String,
    val intervalDays: Int,
    val easeFactor: Double,
    val repetition: Int,
    val lapseCount: Int,
)

@Serializable
data class ErrorResponse(
    val error: String,
)

@Serializable
data class MediaVariantResponse(
    val cardId: Long,
    val audio: List<AudioVariantDto>,
)

@Serializable
data class CreateDeckRequest(
    val code: String,
    val title: String,
)

@Serializable
data class UpdateDeckRequest(
    val code: String,
    val title: String,
)

@Serializable
data class UpsertManageCardRequest(
    val type: CardType = CardType.WORD,
    val frontTextPl: String,
    val frontTranscript: String? = null,
    val backRuTranslation: String = "",
    val backRuPronHint: String? = null,
    val dateIso: String? = null,
    val imagePath: String? = null,
    val historyAnswerPl: String? = null,
    val historyAnswerRu: String? = null,
    val tags: List<String> = emptyList(),
)

@Serializable
data class ImageSearchResultDto(
    val title: String,
    val thumbUrl: String,
    val sourceUrl: String,
    val pageUrl: String,
    val license: String? = null,
    val author: String? = null,
)

@Serializable
data class ImportImageRequest(
    val url: String,
)

@Serializable
data class ManagedImagePathResponse(
    val path: String,
)

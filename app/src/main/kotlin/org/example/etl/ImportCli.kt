package org.example.etl

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import org.example.config.AppConfig
import org.example.db.AppDatabase
import org.example.db.UpsertAttributionInput
import org.example.db.UpsertAudioInput
import org.example.db.UpsertCardInput
import org.example.model.AudioQuality
import org.example.model.AudioSource
import org.example.model.CardType
import java.nio.file.Files
import java.nio.file.Path
import kotlin.io.path.Path
import kotlin.io.path.bufferedReader

fun main(args: Array<String>) {
    val cli = CliArgs.parse(args)
    val env = System.getenv().toMutableMap()
    if (cli.dbPath != null) {
        env["DB_PATH"] = cli.dbPath.toString()
    }
    if (cli.mediaRoot != null) {
        env["MEDIA_ROOT"] = cli.mediaRoot.toString()
    }

    val config = AppConfig.fromEnv(env)
    val json = Json { ignoreUnknownKeys = true }

    val summary = ImportSummary()

    AppDatabase(config).use { db ->
        cli.manifestPath.bufferedReader().useLines { lines ->
            lines.forEachIndexed { index, rawLine ->
                val line = rawLine.trim()
                if (line.isBlank()) {
                    return@forEachIndexed
                }

                summary.rowsRead += 1

                val row = try {
                    json.decodeFromString<ManifestCardRow>(line)
                } catch (ex: Exception) {
                    summary.errors += 1
                    System.err.println("Line ${index + 1}: failed to decode JSON: ${ex.message}")
                    return@forEachIndexed
                }

                val canonicalKey = row.canonicalKey
                    ?.takeIf { it.isNotBlank() }
                    ?: buildCanonicalKey(row.type, row.frontTextPl, row.backRuTranslation)

                if (cli.dryRun) {
                    summary.cardsUpserted += 1
                    summary.audioUpserted += row.audioVariants.size
                    summary.attributionUpserted += row.sourceAttribution.size
                    return@forEachIndexed
                }

                try {
                    val deckId = db.upsertDeck(row.deckCode, row.deckTitle ?: row.deckCode)
                    val cardId = db.upsertCard(
                        UpsertCardInput(
                            deckId = deckId,
                            type = row.type,
                            canonicalKey = canonicalKey,
                            frontTextPl = row.frontTextPl,
                            frontTranscript = row.frontTranscript,
                            backRuTranslation = row.backRuTranslation,
                            backRuPronHint = row.backRuPronHint,
                            dateIso = row.dateIso,
                            imagePath = row.imagePath,
                            historyAnswerPl = row.historyAnswerPl,
                            historyAnswerRu = row.historyAnswerRu,
                            tags = row.tags,
                        )
                    )
                    summary.cardsUpserted += 1

                    row.audioVariants.forEach { audio ->
                        db.upsertAudio(
                            cardId,
                            UpsertAudioInput(
                                source = audio.source,
                                label = audio.label,
                                path = audio.path,
                                quality = audio.quality,
                                isPrimary = audio.isPrimary,
                                license = audio.license,
                                durationMs = audio.durationMs,
                            )
                        )
                        summary.audioUpserted += 1
                    }

                    row.sourceAttribution.forEach { source ->
                        db.upsertAttribution(
                            cardId,
                            UpsertAttributionInput(
                                sourceName = source.sourceName,
                                sourceRef = source.sourceRef,
                                confidenceScore = source.confidenceScore,
                                licenseText = source.license,
                            )
                        )
                        summary.attributionUpserted += 1
                    }
                } catch (ex: Exception) {
                    summary.errors += 1
                    System.err.println("Line ${index + 1}: import failed: ${ex.message}")
                }
            }
        }
    }

    println(
        "import_summary rowsRead=${summary.rowsRead} cardsUpserted=${summary.cardsUpserted} " +
            "audioUpserted=${summary.audioUpserted} attributionUpserted=${summary.attributionUpserted} errors=${summary.errors}"
    )
}

private fun buildCanonicalKey(type: CardType, front: String, back: String): String {
    val normalizedFront = front.trim().lowercase().replace("\\s+".toRegex(), " ")
    val normalizedBack = back.trim().lowercase().replace("\\s+".toRegex(), " ")
    return "${type.name}|$normalizedFront|$normalizedBack"
}

private data class CliArgs(
    val manifestPath: Path,
    val dbPath: Path?,
    val mediaRoot: Path?,
    val dryRun: Boolean,
) {
    companion object {
        fun parse(args: Array<String>): CliArgs {
            var manifestPath: Path? = null
            var dbPath: Path? = null
            var mediaRoot: Path? = null
            var dryRun = false

            var i = 0
            while (i < args.size) {
                when (args[i]) {
                    "--manifest" -> {
                        manifestPath = Path(args.getOrNull(i + 1) ?: error("--manifest requires value"))
                        i += 2
                    }

                    "--db" -> {
                        dbPath = Path(args.getOrNull(i + 1) ?: error("--db requires value"))
                        i += 2
                    }

                    "--media-root" -> {
                        mediaRoot = Path(args.getOrNull(i + 1) ?: error("--media-root requires value"))
                        i += 2
                    }

                    "--dry-run" -> {
                        dryRun = true
                        i += 1
                    }

                    else -> error("Unknown arg: ${args[i]}")
                }
            }

            val manifest = manifestPath ?: error("Missing required --manifest <path>")
            if (!Files.exists(manifest)) {
                error("Manifest does not exist: $manifest")
            }

            return CliArgs(
                manifestPath = manifest,
                dbPath = dbPath,
                mediaRoot = mediaRoot,
                dryRun = dryRun,
            )
        }
    }
}

@Serializable
data class ManifestCardRow(
    val canonicalKey: String? = null,
    val deckCode: String,
    val deckTitle: String? = null,
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
    val audioVariants: List<ManifestAudio> = emptyList(),
    val sourceAttribution: List<ManifestAttribution> = emptyList(),
)

@Serializable
data class ManifestAudio(
    val source: AudioSource,
    val label: String = "",
    val path: String,
    val quality: AudioQuality = AudioQuality.AUTO,
    val isPrimary: Boolean = false,
    val license: String? = null,
    val durationMs: Int? = null,
)

@Serializable
data class ManifestAttribution(
    val sourceName: String,
    val sourceRef: String,
    val confidenceScore: Double = 1.0,
    val license: String? = null,
)

private data class ImportSummary(
    var rowsRead: Int = 0,
    var cardsUpserted: Int = 0,
    var audioUpserted: Int = 0,
    var attributionUpserted: Int = 0,
    var errors: Int = 0,
)

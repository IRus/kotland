package org.example

import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.engine.cio.CIO
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation as ClientContentNegotiation
import io.ktor.client.request.get
import io.ktor.client.statement.bodyAsText
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.URLBuilder
import io.ktor.serialization.kotlinx.json.json
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationStopped
import io.ktor.server.application.call
import io.ktor.server.application.install
import io.ktor.server.auth.Authentication
import io.ktor.server.auth.OAuthAccessTokenResponse
import io.ktor.server.auth.OAuthServerSettings
import io.ktor.server.auth.authenticate
import io.ktor.server.auth.oauth
import io.ktor.server.auth.principal
import io.ktor.server.auth.session
import io.ktor.server.engine.embeddedServer
import io.ktor.server.netty.Netty
import io.ktor.server.plugins.calllogging.CallLogging
import io.ktor.server.plugins.contentnegotiation.ContentNegotiation
import io.ktor.server.plugins.forwardedheaders.ForwardedHeaders
import io.ktor.server.plugins.forwardedheaders.XForwardedHeaders
import io.ktor.server.http.content.staticResources
import io.ktor.server.plugins.statuspages.StatusPages
import io.ktor.server.request.receive
import io.ktor.server.response.respond
import io.ktor.server.response.respondFile
import io.ktor.server.response.respondRedirect
import io.ktor.server.response.respondResource
import io.ktor.server.routing.delete
import io.ktor.server.routing.get
import io.ktor.server.routing.post
import io.ktor.server.routing.put
import io.ktor.server.routing.route
import io.ktor.server.routing.routing
import io.ktor.server.sessions.SessionTransportTransformerMessageAuthentication
import io.ktor.server.sessions.Sessions
import io.ktor.server.sessions.clear
import io.ktor.server.sessions.cookie
import io.ktor.server.sessions.sessions
import io.ktor.server.sessions.set
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.example.auth.GoogleAuthService
import org.example.config.AppConfig
import org.example.db.AppDatabase
import org.example.db.ManageCardInput
import org.example.db.MutationStatus
import org.example.model.ErrorResponse
import org.example.model.ImageSearchResultDto
import org.example.model.ImportImageRequest
import org.example.model.ManagedImagePathResponse
import org.example.model.MediaVariantResponse
import org.example.model.CreateDeckRequest
import org.example.model.ReviewAnswerRequest
import org.example.model.ReviewQueueResponse
import org.example.model.UpdateDeckRequest
import org.example.model.UpsertManageCardRequest
import org.example.service.Sm2Scheduler
import java.net.URI
import java.nio.file.Files
import java.security.MessageDigest
import java.util.UUID
import kotlin.io.path.isRegularFile
import kotlin.io.path.outputStream

fun main() {
    val config = AppConfig.fromEnv()
    embeddedServer(Netty, host = config.host, port = config.port) {
        learningModule(config)
    }.start(wait = true)
}

fun Application.learningModule(
    config: AppConfig = AppConfig.fromEnv(),
    db: AppDatabase = AppDatabase(config),
    oauthClient: HttpClient = HttpClient(CIO) {
        install(ClientContentNegotiation) {
            json(Json { ignoreUnknownKeys = true })
        }
    },
) {
    val scheduler = Sm2Scheduler()
    val googleAuthService = GoogleAuthService(oauthClient, config)

    install(CallLogging)
    install(ForwardedHeaders)
    install(XForwardedHeaders)
    install(ContentNegotiation) {
        json(Json {
            prettyPrint = false
            isLenient = true
            ignoreUnknownKeys = true
            encodeDefaults = true
        })
    }

    install(StatusPages) {
        exception<Throwable> { call, cause ->
            this@learningModule.environment.log.error("Unhandled exception", cause)
            call.respond(HttpStatusCode.InternalServerError, ErrorResponse("internal_error"))
        }
    }

    install(Sessions) {
        cookie<UserSession>("user_session") {
            cookie.path = "/"
            cookie.httpOnly = true
            cookie.secure = config.sessionSecure
            cookie.extensions["SameSite"] = "lax"
            transform(SessionTransportTransformerMessageAuthentication(config.sessionSecret.toByteArray()))
        }
    }

    install(Authentication) {
        oauth("google-oauth") {
            urlProvider = { config.googleRedirectUri }
            providerLookup = {
                OAuthServerSettings.OAuth2ServerSettings(
                    name = "google",
                    authorizeUrl = "https://accounts.google.com/o/oauth2/v2/auth",
                    accessTokenUrl = "https://oauth2.googleapis.com/token",
                    requestMethod = HttpMethod.Post,
                    clientId = config.googleClientId,
                    clientSecret = config.googleClientSecret,
                    defaultScopes = listOf("openid", "email", "profile"),
                    extraAuthParameters = listOf("access_type" to "online", "prompt" to "select_account"),
                )
            }
            client = oauthClient
        }

        session<UserSession>("session-auth") {
            validate { session ->
                if (session.email.isBlank()) {
                    null
                } else {
                    session
                }
            }
            challenge {
                call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
            }
        }
    }

    routing {
        get("/") {
            call.respondResource("static/index.html")
        }

        get("/healthz") {
            call.respond(mapOf("ok" to true))
        }

        get("/readyz") {
            call.respond(mapOf("ok" to true))
        }

        authenticate("google-oauth") {
            get("/auth/login") {
                // Ktor OAuth plugin handles redirect to provider.
            }

            get("/auth/callback") {
                val principal = call.principal<OAuthAccessTokenResponse.OAuth2>()
                    ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("oauth_failed"))
                val idToken = principal.extraParameters["id_token"]
                    ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("id_token_missing"))

                val identity = try {
                    googleAuthService.verifyIdToken(idToken)
                } catch (ex: Exception) {
                    environment.log.warn("OAuth token verification failed", ex)
                    return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("invalid_token"))
                }

                val role = googleAuthService.resolveRole(identity.email)
                db.upsertUser(identity.email, identity.name, role)

                call.sessions.set(UserSession(email = identity.email, name = identity.name, role = role))
                call.respondRedirect("/#/decks")
            }
        }

        post("/auth/logout") {
            call.sessions.clear<UserSession>()
            call.respond(HttpStatusCode.OK, mapOf("ok" to true))
        }

        authenticate("session-auth") {
            get("/media/{path...}") {
                val parts = call.parameters.getAll("path")
                if (parts.isNullOrEmpty()) {
                    return@get call.respond(HttpStatusCode.BadRequest, ErrorResponse("missing_media_path"))
                }

                val relative = parts.joinToString("/")
                val requested = config.mediaRoot.resolve(relative).normalize()
                if (!requested.startsWith(config.mediaRoot) || !requested.isRegularFile()) {
                    return@get call.respond(HttpStatusCode.NotFound, ErrorResponse("media_not_found"))
                }

                call.respondFile(requested.toFile())
            }

            route("/api") {
                get("/me") {
                    val session = call.principal<UserSession>()
                        ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))

                    val user = db.getUser(session.email)
                        ?: db.upsertUser(session.email, session.name, session.role)

                    call.respond(user)
                }

                get("/decks") {
                    val session = call.principal<UserSession>()
                        ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                    call.respond(db.listDecksWithDue(session.email))
                }

                post("/decks/{code}/reset") {
                    val session = call.principal<UserSession>()
                        ?: return@post call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                    val code = call.parameters["code"]?.trim().orEmpty()
                    val result = db.resetDeckProgress(session.email, code)
                    when (result.status) {
                        MutationStatus.OK -> call.respond(
                            HttpStatusCode.OK,
                            mapOf(
                                "ok" to true,
                                "resetCount" to (result.value ?: 0),
                            ),
                        )
                        MutationStatus.INVALID -> call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_deck_code"))
                        MutationStatus.NOT_FOUND -> call.respond(HttpStatusCode.NotFound, ErrorResponse("deck_not_found"))
                        MutationStatus.FORBIDDEN -> call.respond(HttpStatusCode.Forbidden, ErrorResponse("forbidden"))
                        MutationStatus.CONFLICT -> call.respond(HttpStatusCode.Conflict, ErrorResponse("deck_reset_conflict"))
                    }
                }

                get("/review/next") {
                    val session = call.principal<UserSession>()
                        ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))

                    val deckCode = call.request.queryParameters["deck"]
                    val limit = (call.request.queryParameters["limit"]?.toIntOrNull() ?: 10)
                        .coerceIn(1, 100)
                    val cards = db.getNextCards(session.email, deckCode, limit)
                    call.respond(ReviewQueueResponse(cards))
                }

                post("/review/answer") {
                    val session = call.principal<UserSession>()
                        ?: return@post call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))

                    val request = call.receive<ReviewAnswerRequest>()
                    val card = db.getCard(session.email, request.cardId)
                        ?: return@post call.respond(HttpStatusCode.NotFound, ErrorResponse("card_not_found"))

                    if (card.audioVariants.isEmpty()) {
                        environment.log.warn("Card {} has no audio variants", card.id)
                    }

                    call.respond(db.applyReview(session.email, request, scheduler))
                }

                get("/cards/{id}") {
                    val session = call.principal<UserSession>()
                        ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                    val id = call.parameters["id"]?.toLongOrNull()
                        ?: return@get call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_card_id"))

                    val card = db.getCard(session.email, id)
                        ?: return@get call.respond(HttpStatusCode.NotFound, ErrorResponse("card_not_found"))
                    call.respond(card)
                }

                get("/cards/{id}/audio") {
                    val session = call.principal<UserSession>()
                        ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                    val id = call.parameters["id"]?.toLongOrNull()
                        ?: return@get call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_card_id"))

                    if (db.getCard(session.email, id) == null) {
                        return@get call.respond(HttpStatusCode.NotFound, ErrorResponse("card_not_found"))
                    }
                    val audio = db.getCardAudio(id)
                    call.respond(MediaVariantResponse(cardId = id, audio = audio))
                }

                route("/manage") {
                    get("/decks") {
                        val session = call.principal<UserSession>()
                            ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        call.respond(db.listManageDecks(session.email))
                    }

                    get("/images/search") {
                        val session = call.principal<UserSession>()
                            ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val query = call.request.queryParameters["q"]?.trim().orEmpty()
                        if (query.length < 2) {
                            return@get call.respond(HttpStatusCode.BadRequest, ErrorResponse("search_query_too_short"))
                        }
                        val limit = (call.request.queryParameters["limit"]?.toIntOrNull() ?: 12).coerceIn(1, 20)
                        call.respond(searchCommonsImages(oauthClient, query, limit))
                    }

                    post("/images/import") {
                        val session = call.principal<UserSession>()
                            ?: return@post call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val request = call.receive<ImportImageRequest>()
                        val importedPath = importRemoteImageToLibrary(config, oauthClient, session.email, request.url)
                            ?: return@post call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_image_url"))
                        call.respond(HttpStatusCode.Created, ManagedImagePathResponse(importedPath))
                    }

                    post("/media/upload") {
                        val session = call.principal<UserSession>()
                            ?: return@post call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val uploadedPath = saveUploadedManagedImage(call, config, session.email)
                            ?: return@post call.respond(HttpStatusCode.BadRequest, ErrorResponse("missing_file"))
                        call.respond(HttpStatusCode.Created, ManagedImagePathResponse(uploadedPath))
                    }

                    post("/decks") {
                        val session = call.principal<UserSession>()
                            ?: return@post call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val request = call.receive<CreateDeckRequest>()
                        val result = db.createDeckForUser(session.email, request.code, request.title)
                        when (result.status) {
                            MutationStatus.OK -> call.respond(HttpStatusCode.Created, result.value!!)
                            MutationStatus.CONFLICT -> call.respond(HttpStatusCode.Conflict, ErrorResponse("deck_code_conflict"))
                            MutationStatus.INVALID -> call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_deck_payload"))
                            MutationStatus.FORBIDDEN -> call.respond(HttpStatusCode.Forbidden, ErrorResponse("forbidden"))
                            MutationStatus.NOT_FOUND -> call.respond(HttpStatusCode.NotFound, ErrorResponse("deck_not_found"))
                        }
                    }

                    put("/decks/{id}") {
                        val session = call.principal<UserSession>()
                            ?: return@put call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val deckId = call.parameters["id"]?.toLongOrNull()
                            ?: return@put call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_deck_id"))
                        val request = call.receive<UpdateDeckRequest>()
                        val result = db.updateDeckForUser(session.email, deckId, request.code, request.title)
                        when (result.status) {
                            MutationStatus.OK -> call.respond(result.value!!)
                            MutationStatus.CONFLICT -> call.respond(HttpStatusCode.Conflict, ErrorResponse("deck_code_conflict"))
                            MutationStatus.INVALID -> call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_deck_payload"))
                            MutationStatus.FORBIDDEN -> call.respond(HttpStatusCode.Forbidden, ErrorResponse("forbidden"))
                            MutationStatus.NOT_FOUND -> call.respond(HttpStatusCode.NotFound, ErrorResponse("deck_not_found"))
                        }
                    }

                    delete("/decks/{id}") {
                        val session = call.principal<UserSession>()
                            ?: return@delete call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val deckId = call.parameters["id"]?.toLongOrNull()
                            ?: return@delete call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_deck_id"))
                        when (db.deleteDeckForUser(session.email, deckId)) {
                            MutationStatus.OK -> call.respond(HttpStatusCode.OK, mapOf("ok" to true))
                            MutationStatus.FORBIDDEN -> call.respond(HttpStatusCode.Forbidden, ErrorResponse("forbidden"))
                            MutationStatus.NOT_FOUND -> call.respond(HttpStatusCode.NotFound, ErrorResponse("deck_not_found"))
                            MutationStatus.CONFLICT -> call.respond(HttpStatusCode.Conflict, ErrorResponse("deck_delete_conflict"))
                            MutationStatus.INVALID -> call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_deck_payload"))
                        }
                    }

                    get("/decks/{id}/cards") {
                        val session = call.principal<UserSession>()
                            ?: return@get call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val deckId = call.parameters["id"]?.toLongOrNull()
                            ?: return@get call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_deck_id"))
                        val result = db.listDeckCardsForManage(session.email, deckId)
                        when (result.status) {
                            MutationStatus.OK -> call.respond(result.value!!)
                            MutationStatus.FORBIDDEN -> call.respond(HttpStatusCode.Forbidden, ErrorResponse("forbidden"))
                            MutationStatus.NOT_FOUND -> call.respond(HttpStatusCode.NotFound, ErrorResponse("deck_not_found"))
                            MutationStatus.CONFLICT -> call.respond(HttpStatusCode.Conflict, ErrorResponse("cards_conflict"))
                            MutationStatus.INVALID -> call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_card_payload"))
                        }
                    }

                    post("/decks/{id}/cards") {
                        val session = call.principal<UserSession>()
                            ?: return@post call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val deckId = call.parameters["id"]?.toLongOrNull()
                            ?: return@post call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_deck_id"))
                        val request = call.receive<UpsertManageCardRequest>()
                        val input = ManageCardInput(
                            type = request.type,
                            frontTextPl = request.frontTextPl,
                            frontTranscript = request.frontTranscript,
                            backRuTranslation = request.backRuTranslation,
                            backRuPronHint = request.backRuPronHint,
                            dateIso = request.dateIso,
                            imagePath = request.imagePath,
                            historyAnswerPl = request.historyAnswerPl,
                            historyAnswerRu = request.historyAnswerRu,
                            tags = request.tags,
                        )
                        val result = db.createCardForDeck(session.email, deckId, input)
                        when (result.status) {
                            MutationStatus.OK -> call.respond(HttpStatusCode.Created, result.value!!)
                            MutationStatus.FORBIDDEN -> call.respond(HttpStatusCode.Forbidden, ErrorResponse("forbidden"))
                            MutationStatus.NOT_FOUND -> call.respond(HttpStatusCode.NotFound, ErrorResponse("deck_not_found"))
                            MutationStatus.INVALID -> call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_card_payload"))
                            MutationStatus.CONFLICT -> call.respond(HttpStatusCode.Conflict, ErrorResponse("card_conflict"))
                        }
                    }

                    put("/cards/{id}") {
                        val session = call.principal<UserSession>()
                            ?: return@put call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val cardId = call.parameters["id"]?.toLongOrNull()
                            ?: return@put call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_card_id"))
                        val request = call.receive<UpsertManageCardRequest>()
                        val input = ManageCardInput(
                            type = request.type,
                            frontTextPl = request.frontTextPl,
                            frontTranscript = request.frontTranscript,
                            backRuTranslation = request.backRuTranslation,
                            backRuPronHint = request.backRuPronHint,
                            dateIso = request.dateIso,
                            imagePath = request.imagePath,
                            historyAnswerPl = request.historyAnswerPl,
                            historyAnswerRu = request.historyAnswerRu,
                            tags = request.tags,
                        )
                        val result = db.updateManagedCard(session.email, cardId, input)
                        when (result.status) {
                            MutationStatus.OK -> call.respond(result.value!!)
                            MutationStatus.FORBIDDEN -> call.respond(HttpStatusCode.Forbidden, ErrorResponse("forbidden"))
                            MutationStatus.NOT_FOUND -> call.respond(HttpStatusCode.NotFound, ErrorResponse("card_not_found"))
                            MutationStatus.INVALID -> call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_card_payload"))
                            MutationStatus.CONFLICT -> call.respond(HttpStatusCode.Conflict, ErrorResponse("card_conflict"))
                        }
                    }

                    delete("/cards/{id}") {
                        val session = call.principal<UserSession>()
                            ?: return@delete call.respond(HttpStatusCode.Unauthorized, ErrorResponse("unauthorized"))
                        val cardId = call.parameters["id"]?.toLongOrNull()
                            ?: return@delete call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_card_id"))
                        when (db.deleteManagedCard(session.email, cardId)) {
                            MutationStatus.OK -> call.respond(HttpStatusCode.OK, mapOf("ok" to true))
                            MutationStatus.FORBIDDEN -> call.respond(HttpStatusCode.Forbidden, ErrorResponse("forbidden"))
                            MutationStatus.NOT_FOUND -> call.respond(HttpStatusCode.NotFound, ErrorResponse("card_not_found"))
                            MutationStatus.CONFLICT -> call.respond(HttpStatusCode.Conflict, ErrorResponse("card_conflict"))
                            MutationStatus.INVALID -> call.respond(HttpStatusCode.BadRequest, ErrorResponse("invalid_card_payload"))
                        }
                    }
                }
            }
        }

        staticResources("/static", "static")
    }

    monitor.subscribe(ApplicationStopped) {
        db.close()
        oauthClient.close()
    }
}

@Serializable
data class UserSession(
    val email: String,
    val name: String,
    val role: String,
)

private val allowedImageExtensions = setOf("jpg", "jpeg", "png", "webp", "gif")

private suspend fun searchCommonsImages(client: HttpClient, query: String, limit: Int): List<ImageSearchResultDto> {
    val url = URLBuilder("https://commons.wikimedia.org/w/api.php").apply {
        parameters.append("action", "query")
        parameters.append("generator", "search")
        parameters.append("gsrnamespace", "6")
        parameters.append("gsrlimit", limit.toString())
        parameters.append("gsrsearch", query)
        parameters.append("prop", "imageinfo")
        parameters.append("iiprop", "url|extmetadata")
        parameters.append("iiurlwidth", "640")
        parameters.append("format", "json")
        parameters.append("formatversion", "2")
    }.buildString()

    val raw = client.get(url).bodyAsText()
    val root = Json.parseToJsonElement(raw).jsonObject
    val pages = root["query"]?.jsonObject?.get("pages")?.jsonArray ?: return emptyList()

    return pages.mapNotNull { pageElement ->
        val page = pageElement.jsonObject
        val title = page["title"]?.jsonPrimitive?.contentOrNull ?: return@mapNotNull null
        val imageInfo = page["imageinfo"]?.jsonArray?.firstOrNull()?.jsonObject ?: return@mapNotNull null
        val sourceUrl = imageInfo["url"]?.jsonPrimitive?.contentOrNull ?: return@mapNotNull null
        val thumbUrl = imageInfo["thumburl"]?.jsonPrimitive?.contentOrNull ?: sourceUrl
        val pageUrl = imageInfo["descriptionurl"]?.jsonPrimitive?.contentOrNull
            ?: "https://commons.wikimedia.org/wiki/${title.replace(' ', '_')}"
        val extMeta = imageInfo["extmetadata"]?.jsonObject
        val license = extMeta
            ?.get("LicenseShortName")
            ?.jsonObject
            ?.get("value")
            ?.jsonPrimitive
            ?.contentOrNull
            ?.sanitizeHtml()
        val author = extMeta
            ?.get("Artist")
            ?.jsonObject
            ?.get("value")
            ?.jsonPrimitive
            ?.contentOrNull
            ?.sanitizeHtml()

        ImageSearchResultDto(
            title = title,
            thumbUrl = thumbUrl,
            sourceUrl = sourceUrl,
            pageUrl = pageUrl,
            license = license,
            author = author,
        )
    }
}

private suspend fun importRemoteImageToLibrary(
    config: AppConfig,
    client: HttpClient,
    email: String,
    remoteUrl: String,
): String? {
    val parsed = runCatching { URI(remoteUrl.trim()) }.getOrNull() ?: return null
    val scheme = parsed.scheme?.lowercase() ?: return null
    if (scheme != "http" && scheme != "https") {
        return null
    }

    val host = parsed.host?.lowercase() ?: return null
    if (!host.endsWith(".wikimedia.org")) {
        return null
    }

    val responseBytes = runCatching {
        client.get(parsed.toString()).body<ByteArray>()
    }.getOrNull() ?: return null
    if (responseBytes.isEmpty() || responseBytes.size > 15 * 1024 * 1024) {
        return null
    }

    val extFromPath = parsed.path?.substringAfterLast('.', "").orEmpty().lowercase()
    val ext = if (extFromPath in allowedImageExtensions) extFromPath else "jpg"
    val userSlug = emailToSafePathSegment(email)
    val hash = sha256Hex(parsed.toString()).take(20)
    val relative = "images/managed/$userSlug/wiki-$hash.$ext"
    val target = config.mediaRoot.resolve(relative).normalize()
    if (!target.startsWith(config.mediaRoot)) {
        return null
    }

    target.parent?.let { Files.createDirectories(it) }
    target.outputStream().use { output ->
        output.write(responseBytes)
    }
    return relative
}

private suspend fun saveUploadedManagedImage(call: io.ktor.server.application.ApplicationCall, config: AppConfig, email: String): String? {
    val bytes = runCatching { call.receive<ByteArray>() }.getOrNull() ?: return null
    if (bytes.isEmpty() || bytes.size > 15 * 1024 * 1024) {
        return null
    }

    val userSlug = emailToSafePathSegment(email)
    val ext = sanitizeImageExtension(call.request.headers["X-File-Name"])
    val fileName = "upload-${System.currentTimeMillis()}-${UUID.randomUUID().toString().take(8)}.$ext"
    val relative = "images/managed/$userSlug/$fileName"
    val target = config.mediaRoot.resolve(relative).normalize()
    if (!target.startsWith(config.mediaRoot)) {
        return null
    }

    target.parent?.let { Files.createDirectories(it) }
    target.outputStream().use { output ->
        output.write(bytes)
    }

    return relative
}

private fun sanitizeImageExtension(originalFileName: String?): String {
    val ext = originalFileName
        ?.substringAfterLast('.', "")
        ?.lowercase()
        ?.trim()
        .orEmpty()
    return if (ext in allowedImageExtensions) ext else "jpg"
}

private fun emailToSafePathSegment(email: String): String {
    return email
        .trim()
        .lowercase()
        .replace(Regex("[^a-z0-9]+"), "-")
        .trim('-')
        .ifBlank { "user" }
}

private fun sha256Hex(text: String): String {
    val digest = MessageDigest.getInstance("SHA-256")
    return digest.digest(text.toByteArray()).joinToString("") { b -> "%02x".format(b) }
}

private fun String.sanitizeHtml(): String {
    return this
        .replace(Regex("<[^>]+>"), " ")
        .replace("&nbsp;", " ")
        .replace(Regex("\\s+"), " ")
        .trim()
}

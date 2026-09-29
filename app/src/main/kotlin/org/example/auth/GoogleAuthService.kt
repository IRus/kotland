package org.example.auth

import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.request.get
import io.ktor.client.request.parameter
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import org.example.config.AppConfig
import java.time.Instant

class GoogleAuthService(
    private val httpClient: HttpClient,
    private val config: AppConfig,
) {
    suspend fun verifyIdToken(idToken: String): GoogleIdentity {
        val tokenInfo = httpClient.get("https://oauth2.googleapis.com/tokeninfo") {
            parameter("id_token", idToken)
        }.body<GoogleTokenInfo>()

        if (!tokenInfo.isValidIssuer()) {
            throw IllegalArgumentException("Unexpected token issuer")
        }

        if (tokenInfo.aud != config.googleClientId) {
            throw IllegalArgumentException("Token audience mismatch")
        }

        val exp = tokenInfo.exp?.toLongOrNull() ?: throw IllegalArgumentException("Token has no exp")
        if (Instant.ofEpochSecond(exp).isBefore(Instant.now())) {
            throw IllegalArgumentException("Token expired")
        }

        val email = tokenInfo.email?.trim()?.lowercase().orEmpty()
        if (email.isBlank()) {
            throw IllegalArgumentException("Token has no email")
        }

        if (tokenInfo.emailVerified == "false") {
            throw IllegalArgumentException("Email is not verified")
        }

        return GoogleIdentity(
            email = email,
            name = tokenInfo.name?.takeIf { it.isNotBlank() } ?: email,
            picture = tokenInfo.picture,
        )
    }

    fun resolveRole(email: String): String {
        return if (config.adminEmails.contains(email.lowercase())) "admin" else "user"
    }
}

@Serializable
data class GoogleTokenInfo(
    val iss: String? = null,
    val aud: String? = null,
    val exp: String? = null,
    val email: String? = null,
    @SerialName("email_verified")
    val emailVerified: String? = null,
    val name: String? = null,
    val picture: String? = null,
) {
    fun isValidIssuer(): Boolean {
        return iss == "accounts.google.com" || iss == "https://accounts.google.com"
    }
}

data class GoogleIdentity(
    val email: String,
    val name: String,
    val picture: String? = null,
)

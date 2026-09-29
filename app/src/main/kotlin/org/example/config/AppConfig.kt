package org.example.config

import io.heapy.komok.tech.config.dotenv.dotenv
import java.nio.file.Path
import kotlin.io.path.Path

data class AppConfig(
    val host: String,
    val port: Int,
    val dbPath: Path,
    val mediaRoot: Path,
    val googleClientId: String,
    val googleClientSecret: String,
    val googleRedirectUri: String,
    val sessionSecret: String,
    val sessionSecure: Boolean,
    val adminEmails: Set<String>,
) {
    companion object {
        fun fromEnv(env: Map<String, String> = dotenv().properties + System.getenv()): AppConfig {
            return AppConfig(
                host = env["HOST"] ?: "0.0.0.0",
                port = env["PORT"]?.toIntOrNull() ?: 8080,
                dbPath = Path(env["DB_PATH"] ?: "/data/db/app.sqlite"),
                mediaRoot = Path(env["MEDIA_ROOT"] ?: "/data/library/media"),
                googleClientId = env["GOOGLE_CLIENT_ID"] ?: "",
                googleClientSecret = env["GOOGLE_CLIENT_SECRET"] ?: "",
                googleRedirectUri = env["GOOGLE_REDIRECT_URI"] ?: "http://localhost:8080/auth/callback",
                sessionSecret = env["SESSION_SECRET"] ?: "dev-session-secret-change-me",
                sessionSecure = (env["SESSION_SECURE"] ?: "false").toBooleanStrictOrNull() ?: false,
                adminEmails = (env["ADMIN_EMAILS"] ?: "")
                    .split(',')
                    .map { it.trim().lowercase() }
                    .filter { it.isNotBlank() }
                    .toSet(),
            )
        }
    }
}

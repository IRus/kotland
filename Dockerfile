# syntax=docker/dockerfile:1
FROM gradle:9.3.0-jdk21 AS build
WORKDIR /workspace
COPY . .
RUN ./gradlew :app:installDist --no-daemon

FROM eclipse-temurin:21-jre
WORKDIR /opt/app
COPY --from=build /workspace/app/build/install/app /opt/app
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/opt/app/bin/app"]

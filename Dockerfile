# syntax=docker/dockerfile:1
# The build output is plain JVM bytecode, so compile natively instead of under QEMU for every target platform.
FROM --platform=$BUILDPLATFORM gradle:9.3.0-jdk21 AS build
WORKDIR /workspace
COPY . .
RUN ./gradlew :app:installDist --no-daemon

FROM eclipse-temurin:21-jre
WORKDIR /opt/app
COPY --from=build /workspace/app/build/install/app /opt/app
ENV PORT=8080
EXPOSE 8080
ENTRYPOINT ["/opt/app/bin/app"]

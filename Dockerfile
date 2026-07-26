# syntax=docker/dockerfile:1.7

# =============================================================================
# Stage 1: Build the Spring Boot jar and split it into layers
# =============================================================================

# Pinned to BUILDPLATFORM: the jar is architecture-independent, so on a
# multi-arch build this compiles once natively instead of once per target
# under QEMU emulation. Only the runtime stages below need the target arch.
FROM --platform=$BUILDPLATFORM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /app

# Resolve dependencies from the poms alone, so this layer is invalidated only
# when a pom changes and everyday source edits skip resolution entirely.
# The cache mount keeps ~/.m2 out of the image.
COPY pom.xml ./
COPY bot/pom.xml bot/
RUN --mount=type=cache,target=/root/.m2,sharing=locked \
    mvn -B -q -pl bot dependency:go-offline

COPY bot/src bot/src
RUN --mount=type=cache,target=/root/.m2,sharing=locked \
    mvn -B -q -pl bot package -DskipTests

# Split the fat jar along its layer index. Third-party dependencies are 60 MB
# and change when a pom changes; application classes are under a megabyte and
# change every commit. Copied separately below, a code-only rebuild produces
# and pushes ~700 KB instead of the whole 63 MB jar.
RUN cd bot/target \
    && mv "$(ls bot-*.jar | grep -v original)" app.jar \
    && java -Djarmode=tools -jar app.jar extract --layers --launcher --destination /layers

# =============================================================================
# Stage 2: Assemble a Java runtime holding only the modules this app uses
# =============================================================================
FROM eclipse-temurin:21-jdk-alpine AS jre-build

# A stock JRE image is ~200 MB: every JDK module, plus fonts and GnuPG the bot
# never touches. jlink builds a runtime containing just the modules below.
#
# jdeps cannot walk a Spring Boot fat jar reliably, so this set is curated and
# verified by booting the application. The ones easiest to drop by mistake:
#   java.desktop     — java.beans.Introspector, used by Spring bean binding
#   java.instrument  — Spring AOP and instrumentation
#   jdk.unsupported  — sun.misc.Unsafe, required by Netty and Reactor
#   jdk.crypto.ec    — ECDHE cipher suites; without it every HTTPS call fails
#   java.naming      — JNDI lookups inside Hibernate and the JDBC driver
#   jdk.localedata   — Russian locale data for MessageFormat
RUN jlink \
      --add-modules java.base,java.compiler,java.desktop,java.instrument,java.logging,java.management,java.naming,java.net.http,java.prefs,java.rmi,java.scripting,java.security.jgss,java.security.sasl,java.sql,java.sql.rowset,java.transaction.xa,java.xml,java.xml.crypto,jdk.crypto.cryptoki,jdk.crypto.ec,jdk.httpserver,jdk.jfr,jdk.management,jdk.unsupported,jdk.zipfs,jdk.localedata \
      --include-locales=en,ru \
      --strip-debug \
      --no-man-pages \
      --no-header-files \
      --compress=zip-6 \
      --output /javaruntime

# =============================================================================
# Stage 3: Runtime
# =============================================================================
FROM alpine:3.21

ENV JAVA_HOME=/opt/java \
    PATH="/opt/java/bin:${PATH}" \
    LANG=C.UTF-8

COPY --from=jre-build /javaruntime "$JAVA_HOME"

RUN addgroup -S bot && adduser -S -G bot bot
WORKDIR /app
USER bot

# Ordered least- to most-frequently changed, so a code-only rebuild reuses
# every layer above the last one.
COPY --from=build --chown=bot:bot /layers/dependencies/ ./
COPY --from=build --chown=bot:bot /layers/spring-boot-loader/ ./
COPY --from=build --chown=bot:bot /layers/snapshot-dependencies/ ./
COPY --from=build --chown=bot:bot /layers/application/ ./

EXPOSE 8080

# BusyBox wget rather than curl: it is already in the base image, so the health
# check costs nothing instead of an apk layer.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=120s \
    CMD wget -q --spider http://localhost:8080/actuator/health || exit 1

# MaxRAMPercentage sizes the heap from the container limit rather than the
# host's memory.
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", \
            "org.springframework.boot.loader.launch.JarLauncher"]

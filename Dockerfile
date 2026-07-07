# =============================================================================
# Stage 1: Build Java application (Spring Boot fat JAR)
# =============================================================================
FROM maven:3.9-eclipse-temurin-21 AS java-build
COPY pom.xml /app/
COPY bot/pom.xml /app/bot/
WORKDIR /app
RUN --mount=type=cache,target=/root/.m2 mvn dependency:go-offline -pl bot -B
COPY bot/src /app/bot/src
RUN --mount=type=cache,target=/root/.m2 mvn package -pl bot -DskipTests -B

# =============================================================================
# Stage 2: Runtime image — lightweight JRE only
# =============================================================================
FROM eclipse-temurin:21-jre-alpine
RUN apk add --no-cache curl
COPY --from=java-build /app/bot/target/bot-*.jar /app/bot.jar
WORKDIR /app
ENTRYPOINT ["java", "-jar", "/app/bot.jar"]

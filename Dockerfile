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
# Stage 2: Fetch pre-built MCP server (mcp-remnawave)
#
# Downloads mcp-release.zip from the latest GitHub release on
# mitetenov/mcp-remnawave (a fork with CI that builds + publishes dist/).
# The dist/ is a single-file tsup bundle — no npm install / node_modules needed.
# =============================================================================
FROM alpine:3.19 AS mcp-build
RUN apk add --no-cache curl unzip

ARG MCP_OWNER=mitetenov
ARG MCP_REPO=mcp-remnawave
ARG MCP_VERSION=v1.2.1

WORKDIR /mcp-remnawave

# Download the pre-built MCP release at the specified version.
RUN set -eux; \
    MCP_VERSION=${MCP_VERSION}; \
    ASSET_URL="https://github.com/${MCP_OWNER}/${MCP_REPO}/releases/download/${MCP_VERSION}/mcp-release.zip"; \
    echo "Downloading MCP ${MCP_VERSION} from ${ASSET_URL}"; \
    curl -fsSL "${ASSET_URL}" -o mcp-release.zip; \
    echo "Extracting..."; \
    unzip -q mcp-release.zip -d dist; \
    rm -f mcp-release.zip; \
    echo "MCP build ready at /mcp-remnawave/dist:"; \
    ls -la dist/

# =============================================================================
# Stage 3: Runtime image — lightweight JRE + nodejs (for the MCP server)
# =============================================================================
FROM eclipse-temurin:21-jre-alpine
RUN apk add --no-cache nodejs curl
COPY --from=java-build /app/bot/target/bot-*.jar /app/bot.jar
COPY --from=mcp-build /mcp-remnawave/dist /mcp-remnawave/dist
WORKDIR /app
ENTRYPOINT ["java", "-jar", "/app/bot.jar"]

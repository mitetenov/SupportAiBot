FROM maven:3.9-eclipse-temurin-21 AS java-build
COPY pom.xml /app/
COPY bot/pom.xml /app/bot/
WORKDIR /app
RUN --mount=type=cache,target=/root/.m2 mvn dependency:go-offline -pl bot -B
COPY bot/src /app/bot/src
RUN --mount=type=cache,target=/root/.m2 mvn package -pl bot -DskipTests -B

FROM node:22-slim AS mcp-build
ARG MCP_REF=main
RUN apt-get update && apt-get install -y git \
    && git clone --depth 1 --branch ${MCP_REF} https://github.com/TrackLine/mcp-remnawave.git /mcp-remnawave \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /mcp-remnawave
RUN --mount=type=cache,target=/root/.npm npm install \
    && npm run build \
    && npm prune --production \
    && npm cache clean --force

FROM eclipse-temurin:21-jre-alpine
RUN apk add --no-cache nodejs curl
COPY --from=java-build /app/bot/target/bot-*.jar /app/bot.jar
COPY --from=mcp-build /mcp-remnawave/dist /mcp-remnawave/dist
COPY --from=mcp-build /mcp-remnawave/node_modules /mcp-remnawave/node_modules
WORKDIR /app
ENTRYPOINT ["java", "-jar", "/app/bot.jar"]
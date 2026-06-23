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
# Stage 2: Build MCP server (mcp-remnawave)
#
# Downloads the latest release source tarball from GitHub via Node's native
# fetch() (no curl/wget/git needed).  Still runs npm install + npm run build
# because upstream releases only distribute source (no pre-built artifacts).
#
# Once the fork CI (mitenov/mcp-remnawave) is live and publishing pre-built
# archives, this stage can be replaced with a single curl + unzip.
# =============================================================================
FROM node:22-slim AS mcp-build

ARG MCP_OWNER=TrackLine
ARG MCP_REPO=mcp-remnawave
ARG MCP_VERSION=latest

WORKDIR /mcp-remnawave

# Use Node.js 22 native fetch() to download the latest release tarball.
# No apt-get needed — the image stays minimal.
RUN <<EOT node
const { execSync } = require('child_process');
const fs = require('fs');

const owner = '${MCP_OWNER}';
const repo = '${MCP_REPO}';
const version = '${MCP_VERSION}';

async function main() {
  let tarballUrl;
  if (version === 'latest') {
    const url = 'https://api.github.com/repos/' + owner + '/' + repo + '/releases/latest';
    const res = await fetch(url, { headers: { 'Accept': 'application/vnd.github+json' } });
    if (!res.ok) throw new Error('GitHub API ' + res.status + ': ' + res.statusText);
    const data = await res.json();
    const tag = data.tag_name || 'main';
    console.log('Latest release tag:', tag);
    tarballUrl = 'https://api.github.com/repos/' + owner + '/' + repo + '/tarball/' + tag;
  } else {
    tarballUrl = 'https://api.github.com/repos/' + owner + '/' + repo + '/tarball/' + version;
  }
  console.log('Downloading MCP from', tarballUrl);
  const tarballRes = await fetch(tarballUrl);
  if (!tarballRes.ok) throw new Error('Download failed: ' + tarballRes.status);
  const buffer = Buffer.from(await tarballRes.arrayBuffer());
  const tmp = '/tmp/mcp.tar.gz';
  fs.writeFileSync(tmp, buffer);
  console.log('Downloaded', buffer.length, 'bytes');
  execSync('tar xzf ' + tmp + ' --strip-components=1 -C /mcp-remnawave', { stdio: 'inherit' });
  fs.unlinkSync(tmp);
  console.log('Extracted to /mcp-remnawave');
}

main().catch(e => { console.error(e); process.exit(1); });
EOT

# Install + build + prune + clean in one layer for atomicity.
# --mount=type=cache persists /root/.npm between builds (BuildKit cache).
RUN --mount=type=cache,target=/root/.npm \
    npm install && \
    npm run build && \
    npm prune --production && \
    npm cache clean --force

# =============================================================================
# Stage 3: Runtime image — lightweight JRE + nodejs (for the MCP server)
# =============================================================================
FROM eclipse-temurin:21-jre-alpine
RUN apk add --no-cache nodejs curl
COPY --from=java-build /app/bot/target/bot-*.jar /app/bot.jar
COPY --from=mcp-build /mcp-remnawave/dist /mcp-remnawave/dist
COPY --from=mcp-build /mcp-remnawave/node_modules /mcp-remnawave/node_modules
WORKDIR /app
ENTRYPOINT ["java", "-jar", "/app/bot.jar"]

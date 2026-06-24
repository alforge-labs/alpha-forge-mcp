# Dockerfile for Glama MCP-server introspection (https://glama.ai/mcp/servers).
#
# alpha-forge-mcp is a thin stdio MCP wrapper that shells out to the commercial
# AlphaForge `forge` CLI at tool-call time. The ForgeClient is created lazily, so
# the server STARTS and answers introspection (initialize / list_tools — 17 tools,
# resources, prompts) WITHOUT the forge binary present (forge_status simply reports
# binary_found=false). Glama only needs the server to start and respond to
# introspection, so this image installs just the OSS package (its only runtime
# dependency is `mcp`); the closed-source forge binary is intentionally not bundled.
FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

# stdio transport — Glama connects over stdio to run introspection.
ENTRYPOINT ["alpha-forge-mcp"]

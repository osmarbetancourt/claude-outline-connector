# ---------- build stage ----------
FROM python:3.11-slim AS builder

WORKDIR /app

# Install uv for fast, reproducible dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml LICENSE README.md ./
COPY src/ src/

# Install production dependencies into an isolated venv
RUN uv sync --no-dev --no-editable

# ---------- runtime stage ----------
FROM python:3.11-slim

WORKDIR /app

# Install git, gh CLI, curl, jq — needed by github-mcp execute() tool
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl jq ca-certificates \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Copy only the venv and source — no build tools in the final image
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Activate the venv by prepending its bin to PATH
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8765

CMD ["python", "-m", "outline_mcp.server"]

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

# Copy only the venv and source — no build tools in the final image
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Activate the venv by prepending its bin to PATH
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8765

CMD ["python", "-m", "outline_mcp.server"]

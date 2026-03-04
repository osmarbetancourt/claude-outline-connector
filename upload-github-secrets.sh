#!/usr/bin/env bash
# Upload .env-style secrets to a GitHub repository environment (Linux/macOS)
# Requirements: GitHub CLI (gh) installed and authenticated with repo write perms
# Usage:
#   ./upload-github-secrets.sh -f .env -r osmarbetancourt/claude-outline-connector -e production

set -euo pipefail
shopt -s extglob  # enable +() patterns used for trimming

ENV_FILE=".env"
REPO="osmarbetancourt/claude-outline-connector"
ENVIRONMENT="production"

usage() {
  cat <<'EOF'
Usage: ./upload-github-secrets.sh [-f env_file] [-r owner/repo] [-e environment]

Options:
  -f  Path to env file (default: .env)
  -r  GitHub repo in owner/name format (default: osmarbetancourt/claude-outline-connector)
  -e  GitHub environment name (default: production)
EOF
}

while getopts "f:r:e:h" opt; do
  case "$opt" in
    f) ENV_FILE="$OPTARG" ;;
    r) REPO="$OPTARG" ;;
    e) ENVIRONMENT="$OPTARG" ;;
    h) usage; exit 0 ;;
    *) usage; exit 1 ;;
  esac
done

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: gh CLI not found. Install from https://cli.github.com/" >&2
  exit 1
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "Error: env file '$ENV_FILE' not found." >&2
  exit 1
fi

echo "Uploading secrets from $ENV_FILE to $REPO (environment: $ENVIRONMENT)"

while IFS= read -r line || [ -n "$line" ]; do
  line=${line%$'\r'}
  line="${line##+([[:space:]])}"
  line="${line%%+([[:space:]])}"
  if [[ -z "$line" || "$line" == \#* ]]; then
    continue
  fi
  if [[ $line =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
    name="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    value=${value%$'\r'}
    echo "Uploading secret: $name"
    gh secret set "$name" --body "$value" --repo "$REPO" --env "$ENVIRONMENT"
  else
    echo "Warning: skipping invalid line: $line" >&2
  fi
done < "$ENV_FILE"

echo "All secrets uploaded to environment '$ENVIRONMENT' in repo '$REPO'."

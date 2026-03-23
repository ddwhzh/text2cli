#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="${SCRIPT_DIR}/.env"
DEFAULT_HOST="127.0.0.1"
DEFAULT_PORT=8770
DEFAULT_DB=".text2cli/workspace.db"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

load_env() {
    if [[ -f "$ENV_FILE" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "$ENV_FILE"
        set +a
    else
        warn ".env file not found at ${ENV_FILE}. Using defaults / shell env."
    fi
}

check_python() {
    local py
    py=$(command -v python3 || true)
    if [[ -z "$py" ]]; then
        error "python3 not found. Please install Python >= 3.11."
        exit 1
    fi
    local ver
    ver=$("$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    local major minor
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if (( major < 3 || (major == 3 && minor < 11) )); then
        error "Python >= 3.11 required, found ${ver}."
        exit 1
    fi
    info "Python ${ver} OK  (${py})"
}

ensure_installed() {
    if ! python3 -c "import text2cli" &>/dev/null; then
        info "Installing text2cli (editable) ..."
        python3 -m pip install -e "." --quiet
    fi
}

check_port() {
    local port="${1:-$DEFAULT_PORT}"
    if command -v lsof &>/dev/null; then
        if lsof -iTCP:"$port" -sTCP:LISTEN -t &>/dev/null; then
            error "Port ${port} is already in use."
            exit 1
        fi
    fi
}

start_local() {
    load_env
    check_python
    ensure_installed

    local host="${T2_HOST:-$DEFAULT_HOST}"
    local port="${T2_PORT:-$DEFAULT_PORT}"
    local db="${T2_DB:-$DEFAULT_DB}"

    mkdir -p "$(dirname "$db")"
    check_port "$port"

    info "Starting text2cli web server ..."
    exec python3 -m text2cli.web --host "$host" --port "$port" --db "$db"
}

start_docker() {
    load_env
    if ! command -v docker &>/dev/null; then
        error "docker not found. Please install Docker."
        exit 1
    fi
    info "Starting with docker compose ..."
    docker compose up -d --build
    info "Services started. Use 'docker compose logs -f' to follow logs."
}

stop_services() {
    if [[ -f "docker-compose.yml" ]] && command -v docker &>/dev/null; then
        info "Stopping docker compose services ..."
        docker compose down
    fi
    local port="${T2_PORT:-$DEFAULT_PORT}"
    if command -v lsof &>/dev/null; then
        local pids
        pids=$(lsof -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
        if [[ -n "$pids" ]]; then
            info "Killing process(es) on port ${port}: ${pids}"
            echo "$pids" | xargs kill -TERM 2>/dev/null || true
        fi
    fi
    info "Stopped."
}

usage() {
    cat <<EOF
Usage: $0 [COMMAND]

Commands:
  (default)     Start locally (Python + pip install)
  --docker      Start with docker compose
  stop          Stop all running services
  --help        Show this help

Environment (via .env or shell):
  T2_HOST       Listen host   (default: ${DEFAULT_HOST})
  T2_PORT       Listen port   (default: ${DEFAULT_PORT})
  T2_DB         SQLite path   (default: ${DEFAULT_DB})
  LLM_API_KEY   LLM API key for agent
  LLM_MODEL     LLM model name
EOF
}

case "${1:-}" in
    --docker)   start_docker ;;
    stop)       load_env; stop_services ;;
    --help|-h)  usage ;;
    "")         start_local ;;
    *)          error "Unknown command: $1"; usage; exit 1 ;;
esac

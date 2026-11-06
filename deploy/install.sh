#!/usr/bin/env bash
# Redaction Service one-command installer.
#
#   curl -fsSL https://github.com/iginkgo-cap/redaction/releases/latest/download/install.sh | sudo bash
#
# Installs (or upgrades) the redaction API, its Postgres mapping store, the PDF masking web page and a
# Caddy gateway with Docker Compose under /opt/redaction. Re-running it upgrades in place and always keeps
# the existing keys, tokens and data. Run with --help for options.
set -euo pipefail

VERSION="v1.0.0-rc.2"                 # set by the release; override with --version
REGISTRY="ghcr.io/iginkgo-cap"   # override with --registry
INSTALL_DIR=/opt/redaction
MIN_CPUS=4
MIN_MEM_MB=7500
MIN_DISK_GB=20

ACTION=install
PURGE=0
FORCE=0
RENDER_DIR=""
DOCKER_MIRROR=""
# Settings that persist in .env; empty means "not given on the command line".
OPT_VERSION="" OPT_REGISTRY="" OPT_DOMAIN="" OPT_HTTP_PORT="" OPT_HTTPS_PORT="" OPT_HF_MIRROR=""

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: install.sh [options]

  --version <tag>        Release to install, e.g. v1.0.0 (default: the release this script came from)
  --domain <name>        Serve HTTPS on this domain with an automatic Let's Encrypt certificate.
                         The domain's DNS must already point at this server. Without it: plain HTTP.
  --http-port <port>     Host port for HTTP (default 80)
  --https-port <port>    Host port for HTTPS, only used with --domain (default 443)
  --hf-mirror <url>      Download models from a Hugging Face mirror, e.g. https://hf-mirror.com
  --docker-mirror <name> Mirror for installing Docker (passed to get.docker.com), e.g. Aliyun
  --registry <prefix>    Pull images from another registry, e.g. registry.example.com/redaction
  --dir <path>           Install directory (default /opt/redaction)
  --force                Skip the CPU / memory / disk checks
  --uninstall            Stop and remove the containers; keeps data and keys
  --purge                With --uninstall: also delete the database, models and the install directory
  --render-only <dir>    Only write the configuration files into <dir> (no Docker); for testing
  -h, --help             Show this help

Re-running the installer upgrades in place. Options you pass are saved; options you leave out keep
their saved values.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --version) OPT_VERSION="${2:?--version needs a value}"; shift 2 ;;
    --domain) OPT_DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
    --http-port) OPT_HTTP_PORT="${2:?--http-port needs a value}"; shift 2 ;;
    --https-port) OPT_HTTPS_PORT="${2:?--https-port needs a value}"; shift 2 ;;
    --hf-mirror) OPT_HF_MIRROR="${2:?--hf-mirror needs a value}"; shift 2 ;;
    --docker-mirror) DOCKER_MIRROR="${2:?--docker-mirror needs a value}"; shift 2 ;;
    --registry) OPT_REGISTRY="${2:?--registry needs a value}"; shift 2 ;;
    --dir) INSTALL_DIR="${2:?--dir needs a value}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --uninstall) ACTION=uninstall; shift ;;
    --purge) PURGE=1; shift ;;
    --render-only) ACTION=render; RENDER_DIR="${2:?--render-only needs a directory}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

# ---------- helpers ----------

rand_b64() { head -c 32 /dev/urandom | base64 | tr -d '\n'; }                       # 32 random bytes, base64
rand_token() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n'; }      # URL-safe
rand_alnum() { local s; s=$(head -c 48 /dev/urandom | base64 | tr -d '+/=\n'); printf '%s' "${s:0:24}"; }
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then printf '%s' "$1" | sha256sum | cut -d' ' -f1
  else printf '%s' "$1" | shasum -a 256 | cut -d' ' -f1; fi
}
is_port() { [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }
compose() { docker compose --project-directory "$INSTALL_DIR" -f "$INSTALL_DIR/docker-compose.yml" "$@"; }
retry() { local n; for n in 1 2 3; do "$@" && return 0; warn "attempt $n failed: $*"; sleep 5; done; return 1; }

# ---------- checks ----------

preflight() {
  [ "$(uname -s)" = Linux ] || die "only Linux servers are supported (found $(uname -s))"
  [ "${EUID:-$(id -u)}" -eq 0 ] || die "run as root, e.g. curl -fsSL <url> | sudo bash"
  case "$(uname -m)" in
    x86_64|amd64|aarch64|arm64) ;;
    *) die "unsupported CPU architecture $(uname -m); amd64 and arm64 are supported" ;;
  esac
  [ "$FORCE" = 1 ] && return 0
  local cpus mem_mb disk_gb
  cpus=$(nproc)
  mem_mb=$(( $(awk '/^MemTotal:/ {print $2}' /proc/meminfo) / 1024 ))
  mkdir -p "$INSTALL_DIR"
  disk_gb=$(( $(df -Pk "$INSTALL_DIR" | awk 'NR==2 {print $4}') / 1024 / 1024 ))
  [ "$cpus" -ge "$MIN_CPUS" ] || die "need at least $MIN_CPUS CPUs, found $cpus (use --force to skip)"
  [ "$mem_mb" -ge "$MIN_MEM_MB" ] || die "need at least 8 GB of memory, found ${mem_mb} MB (use --force to skip)"
  [ "$disk_gb" -ge "$MIN_DISK_GB" ] || die "need at least ${MIN_DISK_GB} GB free under $INSTALL_DIR, found ${disk_gb} GB (use --force to skip)"
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    command -v curl >/dev/null 2>&1 || die "curl is required to install Docker"
    log "Docker not found; installing it with get.docker.com ..."
    local args=()
    [ -n "$DOCKER_MIRROR" ] && args=(--mirror "$DOCKER_MIRROR")
    curl -fsSL https://get.docker.com | sh -s -- "${args[@]}"
  fi
  if command -v systemctl >/dev/null 2>&1; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
  docker info >/dev/null 2>&1 || die "Docker is installed but not running"
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the 'docker compose' plugin)"
}

port_in_use() {
  if command -v ss >/dev/null 2>&1; then ss -ltnH "sport = :$1" | grep -q .
  else (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; fi
}

check_ports() {
  # On upgrade our own gateway holds the ports.
  if docker ps --format '{{.Names}}' | grep -qx redaction-gateway; then return 0; fi
  port_in_use "$HTTP_PORT" && die "port $HTTP_PORT is already in use; free it or pass --http-port"
  if [ -n "$DOMAIN" ] && port_in_use "$HTTPS_PORT"; then die "port $HTTPS_PORT is already in use; free it or pass --https-port"; fi
  return 0
}

# ---------- settings and secrets ----------

load_settings() {
  local env_file="$1"
  if [ -f "$env_file" ]; then
    # shellcheck disable=SC1090
    . "$env_file"
  fi
  REDACTION_VERSION="${OPT_VERSION:-${REDACTION_VERSION:-$VERSION}}"
  REGISTRY="${OPT_REGISTRY:-${IMAGE_REGISTRY:-$REGISTRY}}"
  DOMAIN="${OPT_DOMAIN:-${DOMAIN:-}}"
  HTTP_PORT="${OPT_HTTP_PORT:-${HTTP_PORT:-80}}"
  HTTPS_PORT="${OPT_HTTPS_PORT:-${HTTPS_PORT:-443}}"
  HF_ENDPOINT="${OPT_HF_MIRROR:-${HF_ENDPOINT:-https://huggingface.co}}"
  case "$REDACTION_VERSION" in __*__|"") die "no version to install; pass --version <tag>" ;; esac
  case "$REGISTRY" in *__*__*) die "no image registry configured; pass --registry <prefix>" ;; esac
  is_port "$HTTP_PORT" || die "invalid --http-port: $HTTP_PORT"
  is_port "$HTTPS_PORT" || die "invalid --https-port: $HTTPS_PORT"
  if [ -n "$DOMAIN" ]; then SITE_ADDRESS="$DOMAIN"; else SITE_ADDRESS=":80"; fi
}

new_secrets() {
  POSTGRES_PASSWORD=$(rand_alnum)
  REDACTION_ENCRYPTION_KEY=$(rand_b64)
  REDACTION_HMAC_KEY=$(rand_b64)
  WEB_USER="admin"
  WEB_PASSWORD=$(rand_alnum)
  if [ "$ACTION" = render ]; then
    WEB_PASSWORD_HASH='render-only-not-a-hash'
  else
    WEB_PASSWORD_HASH=$(docker run --rm "caddy:2.8" caddy hash-password --plaintext "$WEB_PASSWORD")
  fi
  NEW_INSTALL=1
}

write_env() {
  local f="$1"
  umask 077
  cat >"$f" <<EOF
# Written by install.sh. Keep a copy of this file somewhere safe: the two keys below encrypt the
# stored values, and without them the database cannot be read.
REDACTION_VERSION='$REDACTION_VERSION'
IMAGE_REGISTRY='$REGISTRY'
DOMAIN='$DOMAIN'
SITE_ADDRESS='$SITE_ADDRESS'
HTTP_PORT='$HTTP_PORT'
HTTPS_PORT='$HTTPS_PORT'
HF_ENDPOINT='$HF_ENDPOINT'
POSTGRES_PASSWORD='$POSTGRES_PASSWORD'
REDACTION_ENCRYPTION_KEY='$REDACTION_ENCRYPTION_KEY'
REDACTION_HMAC_KEY='$REDACTION_HMAC_KEY'
WEB_USER='$WEB_USER'
WEB_PASSWORD_HASH='$WEB_PASSWORD_HASH'
EOF
  chmod 600 "$f"
}

write_namespaces() {
  local f="$1"
  [ -f "$f" ] && return 0
  APP_TOKEN=$(rand_token)
  ADMIN_TOKEN=$(rand_token)
  umask 077
  cat >"$f" <<EOF
# /v2 callers and namespaces (see config/namespaces.example.yaml in the repository).
# Only token hashes are stored here; the tokens were shown once by install.sh and saved in credentials.txt.
callers:
  app: { token_sha256: ["$(sha256 "$APP_TOKEN")"] }
  admin: { token_sha256: ["$(sha256 "$ADMIN_TOKEN")"], admin: true }

namespaces:
  - id: default
    slug: default
    callers: [app, admin]
EOF
  chmod 644 "$f"   # read by the redaction container
}

write_compose() {
  local f="$1" ports
  ports="      - \"\${HTTP_PORT}:80\""
  if [ -n "$DOMAIN" ]; then ports+=$'\n'"      - \"\${HTTPS_PORT}:443\""; fi
  cat >"$f" <<EOF
# Written by install.sh; re-running the installer overwrites it. Settings live in .env.
name: redaction

services:
  postgres:
    image: postgres:16-alpine
    container_name: redaction-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: redaction
      POSTGRES_PASSWORD: \${POSTGRES_PASSWORD}
      POSTGRES_DB: redaction_meta
    command: ["postgres", "-c", "max_connections=200"]
    volumes:
      - redaction-pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "redaction", "-d", "redaction_meta"]
      interval: 10s
      timeout: 5s
      retries: 6

  redaction:
    image: \${IMAGE_REGISTRY}/redaction-service:\${REDACTION_VERSION}
    container_name: redaction
    restart: unless-stopped
    environment:
      REDACTION_DATABASE_URL: postgresql://redaction:\${POSTGRES_PASSWORD}@postgres:5432/redaction_meta
      REDACTION_KEY_SOURCE: local
      REDACTION_ENCRYPTION_KEY: \${REDACTION_ENCRYPTION_KEY}
      REDACTION_HMAC_KEY: \${REDACTION_HMAC_KEY}
      REDACTION_NAMESPACES_FILE: /etc/redaction/namespaces.yaml
      REDACTION_ENABLE_V1: "false"
      REDACTION_DETECTORS: cn_regex,en_regex,knowledgator,gazetteer
      REDACTION_GPU_DEVICE: cpu
      REDACTION_LOG_LEVEL: INFO
      REDACTION_MAX_CONCURRENT_INFERENCE: "4"
      REDACTION_MAX_PENDING_REQUESTS: "6"
      OMP_NUM_THREADS: "2"
      HF_HOME: /models/huggingface
      HF_HUB_OFFLINE: "1"
      TRANSFORMERS_OFFLINE: "1"
    volumes:
      - ./namespaces.yaml:/etc/redaction/namespaces.yaml:ro,Z
      - redaction-models:/models:ro
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:3006/ready"]
      interval: 30s
      timeout: 10s
      start_period: 180s
      retries: 3
    deploy:
      resources:
        limits:
          memory: 6G

  file-masker:
    image: \${IMAGE_REGISTRY}/file-masker:\${REDACTION_VERSION}
    container_name: redaction-file-masker
    restart: unless-stopped
    tmpfs:
      - /tmp:size=512m
    deploy:
      resources:
        limits:
          memory: 2G

  gateway:
    image: caddy:2.8
    container_name: redaction-gateway
    restart: unless-stopped
    environment:
      SITE_ADDRESS: \${SITE_ADDRESS}
      WEB_USER: \${WEB_USER}
      WEB_PASSWORD_HASH: \${WEB_PASSWORD_HASH}
    ports:
${ports}
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro,Z
      - caddy-data:/data
      - caddy-config:/config
    depends_on:
      - redaction
      - file-masker

  # One-off: docker compose --profile setup run --rm models
  models:
    image: \${IMAGE_REGISTRY}/redaction-service:\${REDACTION_VERSION}
    profiles: ["setup"]
    entrypoint: ["python", "scripts/download_models.py", "--target", "/models"]
    environment:
      HF_ENDPOINT: \${HF_ENDPOINT}
    volumes:
      - redaction-models:/models

volumes:
  redaction-pgdata:
    name: redaction-pgdata
  redaction-models:
    name: redaction-models
  caddy-data:
    name: redaction-caddy-data
  caddy-config:
    name: redaction-caddy-config
EOF
}

write_caddyfile() {
  cat >"$1" <<'EOF'
# Written by install.sh; re-running the installer overwrites it.
{
	admin off
}

{$SITE_ADDRESS} {
	encode zstd gzip

	# API: authenticated by the redaction service itself (X-Redaction-Token).
	@api path /v2/*
	handle @api {
		request_body {
			max_size 40MB
		}
		reverse_proxy redaction:3006
	}

	# Never exposed: unauthenticated legacy API, mail mode, health and version endpoints.
	@blocked path /v1/* /mail /mail/* /api/mail /api/mail/* /health /ready /version /docs* /redoc* /openapi.json
	handle @blocked {
		respond 404
	}

	# PDF masking web page.
	handle {
		basic_auth {
			{$WEB_USER} {$WEB_PASSWORD_HASH}
		}
		request_body {
			max_size 110MB
		}
		reverse_proxy file-masker:8080
	}
}
EOF
}

write_backup_script() {
  cat >"$1" <<'EOF'
#!/usr/bin/env bash
# Back up the mapping databases:  bash /opt/redaction/backup.sh [dest dir] [backups to keep]
# Example cron (daily 03:30):      30 3 * * * bash /opt/redaction/backup.sh /var/backups/redaction 14
# Each backup is a directory with one pg_dump file per database. Values are stored encrypted; keep
# /opt/redaction/.env (the keys) separately, never next to the backups.
#
# Restore (stops the API while restoring):
#   docker stop redaction
#   for f in /var/backups/redaction/<backup>/*.dump; do
#     docker exec -i redaction-postgres pg_restore -U redaction -d postgres --clean --if-exists --create < "$f"
#   done
#   docker start redaction
set -euo pipefail
DEST="${1:-/var/backups/redaction}"
KEEP="${2:-14}"
mkdir -p "$DEST" && chmod 700 "$DEST"
dir="$DEST/redaction-$(date +%Y%m%d-%H%M%S)"
mkdir "$dir.tmp"
dbs=$(docker exec redaction-postgres psql -U redaction -d postgres -Atc \
  "SELECT datname FROM pg_database WHERE datname = 'redaction_meta' OR datname LIKE 'rs\_%'")
for db in $dbs; do
  docker exec redaction-postgres pg_dump -U redaction -Fc -d "$db" >"$dir.tmp/$db.dump"
done
mv "$dir.tmp" "$dir"
ls -1dt "$DEST"/redaction-*/ | grep -v '\.tmp/$' | tail -n +"$((KEEP + 1))" | xargs -r rm -rf
echo "backup written: $dir ($(echo "$dbs" | wc -w | tr -d ' ') databases)"
EOF
  chmod 700 "$1"
}

render_all() {
  local dir="$1"
  mkdir -p "$dir"
  chmod 700 "$dir"
  if [ ! -f "$dir/.env" ]; then
    if [ "$ACTION" != render ] && docker volume inspect redaction-pgdata >/dev/null 2>&1; then
      die "found the database volume redaction-pgdata but no $dir/.env. The keys in .env are needed to read it.
       Restore .env from your backup into $dir and re-run, or remove the old data with: docker volume rm redaction-pgdata"
    fi
    load_settings /dev/null
    new_secrets
  else
    load_settings "$dir/.env"
    NEW_INSTALL=0
  fi
  write_env "$dir/.env"
  write_namespaces "$dir/namespaces.yaml"
  write_compose "$dir/docker-compose.yml"
  write_caddyfile "$dir/Caddyfile"
  write_backup_script "$dir/backup.sh"
}

# ---------- actions ----------

wait_ready() {
  log "Waiting for the redaction service to load its models (up to 10 minutes) ..."
  for _ in $(seq 1 120); do
    if docker exec redaction curl -fsS http://localhost:3006/ready >/dev/null 2>&1; then return 0; fi
    sleep 5
  done
  docker logs --tail 50 redaction >&2 || true
  die "the redaction service did not become ready; see the logs above (docker logs redaction)"
}

smoke_test() {
  log "Smoke test ..."
  if [ -n "${APP_TOKEN:-}" ]; then
    local out
    out=$(docker exec redaction curl -fsS -X POST http://localhost:3006/v2/detect \
      -H "X-Redaction-Token: $APP_TOKEN" -H 'Content-Type: application/json' \
      -d '{"namespace":"default","items":[{"id":"t","text":"Contact alice@example.com"}]}') \
      || die "API smoke test failed"
    grep -q '"EMAIL"' <<<"$out" || die "API smoke test did not find the e-mail address: $out"
  fi
  if [ -z "$DOMAIN" ]; then
    local base="http://127.0.0.1:$HTTP_PORT" code
    code=$(curl -s -o /dev/null -w '%{http_code}' "$base/" || true)
    [ "$code" = 401 ] || die "web page check failed: expected HTTP 401 (login required), got ${code:-no response}"
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$base/v2/detect" -H 'Content-Type: application/json' \
      -d '{"namespace":"default","items":[{"id":"t","text":"x"}]}' || true)
    [ "$code" = 401 ] || die "API gateway check failed: expected HTTP 401 without a token, got ${code:-no response}"
    code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$base/v1/detect" || true)
    [ "$code" = 404 ] || die "gateway check failed: /v1 should be blocked (404), got ${code:-no response}"
  fi
}

write_credentials() {
  local f="$INSTALL_DIR/credentials.txt" url
  if [ -n "$DOMAIN" ]; then
    url="https://$DOMAIN"; [ "$HTTPS_PORT" = 443 ] || url="$url:$HTTPS_PORT"
  else
    url="http://<this-server-ip>"; [ "$HTTP_PORT" = 80 ] || url="$url:$HTTP_PORT"
  fi
  umask 077
  cat >"$f" <<EOF
Redaction Service — installed $(date '+%Y-%m-%d %H:%M:%S %Z'), version $REDACTION_VERSION

PDF masking page : $url/
  user           : $WEB_USER
  password       : $WEB_PASSWORD

API base URL     : $url/v2/
  namespace      : default
  app token      : ${APP_TOKEN:-unchanged (namespaces.yaml already existed)}        (header X-Redaction-Token)
  admin token    : ${ADMIN_TOKEN:-unchanged (namespaces.yaml already existed)}      (only for POST /v2/namespaces/remove)

Only hashes of the tokens and the password are stored elsewhere. Keep this file safe, then delete it.
EOF
  chmod 600 "$f"
}

do_install() {
  preflight
  ensure_docker
  render_all "$INSTALL_DIR"
  check_ports
  log "Installing version $REDACTION_VERSION from $REGISTRY into $INSTALL_DIR"
  retry compose pull --quiet postgres redaction file-masker gateway || die "could not pull the images from $REGISTRY"
  log "Downloading models from $HF_ENDPOINT ..."
  retry compose --profile setup run --rm models || die "model download failed; on networks without Hugging Face access, re-run with --hf-mirror https://hf-mirror.com"
  compose up -d --remove-orphans
  compose restart gateway >/dev/null   # pick up a rewritten Caddyfile (up -d does not restart for file changes)
  wait_ready
  smoke_test
  if [ "${NEW_INSTALL:-0}" = 1 ]; then write_credentials; fi

  echo
  log "Done. Version $REDACTION_VERSION is running."
  if [ "${NEW_INSTALL:-0}" = 1 ]; then
    echo
    cat "$INSTALL_DIR/credentials.txt"
    echo
    echo "These credentials were saved to $INSTALL_DIR/credentials.txt (readable by root only)."
  fi
  if [ -z "$DOMAIN" ]; then
    warn "serving plain HTTP: the password and data travel unencrypted. Use --domain for HTTPS, or keep the port behind a VPN / firewall."
  fi
  echo "Back up $INSTALL_DIR/.env now: without its keys the stored data cannot be decrypted."
}

do_uninstall() {
  [ "${EUID:-$(id -u)}" -eq 0 ] || die "run as root"
  [ -f "$INSTALL_DIR/docker-compose.yml" ] || die "nothing installed in $INSTALL_DIR"
  if [ "$PURGE" = 1 ]; then
    compose --profile setup down --volumes --remove-orphans
    rm -rf "$INSTALL_DIR"
    log "Removed the containers, the database, the models and $INSTALL_DIR."
  else
    compose --profile setup down --remove-orphans
    log "Removed the containers. Data and keys are kept in Docker volumes and $INSTALL_DIR; re-run the installer to start again."
  fi
}

case "$ACTION" in
  install) do_install ;;
  uninstall) do_uninstall ;;
  render) INSTALL_DIR="$RENDER_DIR"; render_all "$RENDER_DIR"; log "Configuration written to $RENDER_DIR" ;;
esac

#!/usr/bin/env bash
# =============================================================================
#  AI Job Agent - installer
#
#  Run this INSIDE your Debian/Ubuntu container or virtual machine (NOT on the
#  Proxmox host), as root, from the folder you unzipped:
#
#      cd ai-job-agent
#      bash install.sh
#
#  What it changes (and nothing else):
#    1. Installs: curl, ca-certificates, locales, tzdata, nano (apt).
#    2. Sets the time zone (default Europe/London) and generates English locales.
#    3. Installs Docker Engine + Compose plugin from get.docker.com (if missing).
#    4. Creates /opt/stacks/{ai,jobs,jobagent}, /opt/dockge and /opt/jobagent
#       and copies the project files there. Existing config.json, master.md,
#       answers.yaml and .env files are NEVER overwritten.
#    5. Creates /opt/stacks/jobagent/.env from .env.example (mode 600).
#    6. Starts Ollama (+ Open WebUI), NocoDB and Dockge, and downloads the AI model.
#    7. Builds the Job Finder container. It is only started when .env has been
#       filled in (otherwise you are told what to do next).
#
#  Options:
#    --minimal        skip Open WebUI and Dockge (recommended for 4-8 GB RAM)
#    --model NAME     AI model to download (default: qwen2.5:3b)
#    --no-model       do not download a model now
#    --tz ZONE        time zone, e.g. Europe/London (default)
#    -y               do not ask for confirmation
# =============================================================================
set -euo pipefail

MODEL="qwen2.5:3b"
TZONE="Europe/London"
MINIMAL=0
PULL_MODEL=1
ASSUME_YES=0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [ $# -gt 0 ]; do
  case "$1" in
    --minimal)  MINIMAL=1 ;;
    --model)    MODEL="${2:?--model needs a name}"; shift ;;
    --no-model) PULL_MODEL=0 ;;
    --tz)       TZONE="${2:?--tz needs a zone}"; shift ;;
    -y|--yes)   ASSUME_YES=1 ;;
    -h|--help)  sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (use --help)"; exit 1 ;;
  esac
  shift
done

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32mOK\033[0m %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mERROR:\033[0m %s\n' "$*"; exit 1; }

# ---------------------------------------------------------------- safety checks
[ "$(id -u)" -eq 0 ] || die "Please run as root (in a Proxmox container you already are root)."
[ -f "$SRC/stacks/jobagent/compose.yaml" ] || die "Run this from the unzipped ai-job-agent folder."
if command -v pveversion >/dev/null 2>&1; then
  die "This looks like the Proxmox HOST. Run the installer inside the container: pct enter <ID>"
fi
. /etc/os-release 2>/dev/null || true
case "${ID:-}" in
  debian|ubuntu) ok "Operating system: ${PRETTY_NAME:-$ID}" ;;
  *) warn "Untested operating system '${ID:-unknown}'. Debian 12/13 or Ubuntu 22.04/24.04 recommended." ;;
esac

MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
DISK_GB=$(df -Pm / | awk 'NR==2 {print int($4/1024)}')
ok "Memory: ${MEM_MB} MB, free disk: ${DISK_GB} GB"
[ "$MEM_MB" -lt 3500 ] && warn "Less than 4 GB RAM: use --minimal and a 1.5B model (see handbook, chapter 3)."
[ "$DISK_GB" -lt 15 ] && warn "Less than 15 GB free disk: models and images may not fit."

echo
echo "This will install Docker and the AI Job Agent into /opt on THIS machine."
echo "Model: $MODEL   Time zone: $TZONE   Minimal: $([ $MINIMAL = 1 ] && echo yes || echo no)"
if [ "$ASSUME_YES" -ne 1 ]; then
  read -r -p "Continue? [y/N] " answer
  case "$answer" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0 ;; esac
fi

# ---------------------------------------------------------------- 1-2 base system
say "Installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates locales tzdata nano >/dev/null
ok "curl, ca-certificates, locales, tzdata, nano"

say "Setting time zone and locales"
ln -sf "/usr/share/zoneinfo/$TZONE" /etc/localtime && echo "$TZONE" > /etc/timezone
sed -i 's/^# *\(en_GB.UTF-8 UTF-8\)/\1/; s/^# *\(en_US.UTF-8 UTF-8\)/\1/' /etc/locale.gen
locale-gen >/dev/null 2>&1 || true
ok "Time zone $TZONE, locales en_GB/en_US generated"

# ---------------------------------------------------------------- 3 docker
say "Installing Docker"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ok "Docker already installed: $(docker --version)"
else
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
  sh /tmp/get-docker.sh >/dev/null
  rm -f /tmp/get-docker.sh
  ok "$(docker --version)"
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker info >/dev/null 2>&1 || die "Docker is installed but not running. In Proxmox, enable 'nesting' for this container (handbook, Stage D)."

# ---------------------------------------------------------------- 4 files
say "Copying project files"
copy_new() {  # copy_new SOURCE DEST : copy only if DEST does not exist yet
  if [ -e "$2" ]; then warn "kept your existing $2"; else cp "$1" "$2"; ok "$2"; fi
}
mkdir -p /opt/stacks/ai /opt/stacks/jobs /opt/stacks/jobagent /opt/dockge \
         /opt/jobagent/app /opt/jobagent/cv /opt/jobagent/state /opt/jobagent/output /opt/jobagent/tools

cp "$SRC/stacks/ai/compose.yaml"         /opt/stacks/ai/compose.yaml
cp "$SRC/stacks/jobs/compose.yaml"       /opt/stacks/jobs/compose.yaml
cp "$SRC/stacks/jobagent/compose.yaml"   /opt/stacks/jobagent/compose.yaml
cp "$SRC/stacks/jobagent/Dockerfile"     /opt/stacks/jobagent/Dockerfile
cp "$SRC/stacks/jobagent/.env.example"   /opt/stacks/jobagent/.env.example
cp "$SRC/dockge/compose.yaml"            /opt/dockge/compose.yaml
cp "$SRC/jobagent/app/finder.py"         /opt/jobagent/app/finder.py
cp "$SRC"/jobagent/tools/*.py            /opt/jobagent/tools/
ok "program files and compose files"
copy_new "$SRC/jobagent/app/config.json" /opt/jobagent/app/config.json
copy_new "$SRC/jobagent/cv/master.md"    /opt/jobagent/cv/master.md
copy_new "$SRC/jobagent/cv/answers.yaml" /opt/jobagent/cv/answers.yaml

# ---------------------------------------------------------------- 5 .env
ENV_FILE=/opt/stacks/jobagent/.env
copy_new /opt/stacks/jobagent/.env.example "$ENV_FILE"
chmod 600 "$ENV_FILE"
chown -R 1000:1000 /opt/jobagent   # uid 1000 = the user inside the optional n8n container
ok ".env permissions set to 600 (only root can read it)"

# ---------------------------------------------------------------- 6 services
say "Starting the AI stack"
cd /opt/stacks/ai
if [ "$MINIMAL" -eq 1 ]; then docker compose up -d ollama; else docker compose up -d; fi
ok "Ollama running$([ $MINIMAL = 1 ] || echo ', Open WebUI on port 3000')"

say "Starting the tracker (NocoDB)"
cd /opt/stacks/jobs && docker compose up -d
ok "NocoDB on port 8081"

if [ "$MINIMAL" -eq 0 ]; then
  say "Starting Dockge"
  cd /opt/dockge && docker compose up -d
  ok "Dockge on port 5001"
fi

if [ "$PULL_MODEL" -eq 1 ]; then
  say "Downloading AI model $MODEL (this can take 5-30 minutes)"
  for i in 1 2 3 4 5 6; do docker exec ollama ollama list >/dev/null 2>&1 && break; sleep 5; done
  docker exec ollama ollama pull "$MODEL"
  ok "Model $MODEL ready"
  if [ "$MODEL" != "qwen2.5:3b" ]; then
    sed -i "s|^SCORING_MODEL=.*|SCORING_MODEL=$MODEL|" "$ENV_FILE"
    ok "SCORING_MODEL set to $MODEL in .env"
  fi
fi

# ---------------------------------------------------------------- 7 job finder
say "Building the Job Finder container"
cd /opt/stacks/jobagent && docker compose build -q
ok "Image built"

MISSING=$(grep -E '^(REED_API_KEY|TELEGRAM_TOKEN|TELEGRAM_CHAT_ID|NOCODB_TOKEN|NOCODB_TABLE_ID)=.*YOUR_' "$ENV_FILE" | cut -d= -f1 | tr '\n' ' ' || true)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$MISSING" ]; then
  docker compose up -d
  ok "Job Finder started (scheduler mode)"
else
  warn "Job Finder NOT started yet. Still to fill in $ENV_FILE: $MISSING"
fi

cat <<EOF

=====================================================================
 Installation finished.
   NocoDB tracker : http://${IP:-YOUR_SERVER_IP}:8081
$( [ "$MINIMAL" -eq 0 ] && echo "   Open WebUI     : http://${IP:-YOUR_SERVER_IP}:3000" )
$( [ "$MINIMAL" -eq 0 ] && echo "   Dockge         : http://${IP:-YOUR_SERVER_IP}:5001" )
 (With the NAT network option, use the Proxmox host's address instead.)

 Next steps (handbook, Stage F-J):
   1. Create the NocoDB table and API token          (Stage F2)
   2. Create the Telegram bot                         (Stage G)
   3. Edit the secrets:   nano $ENV_FILE
   4. Edit your profile:  nano /opt/jobagent/cv/master.md
   5. Start / restart:    cd /opt/stacks/jobagent && docker compose up -d --force-recreate
   6. Check everything:   docker exec jobagent python /data/tools/check_setup.py --send
=====================================================================
EOF

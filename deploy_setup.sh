#!/usr/bin/env bash

set -euo pipefail

DEFAULT_OWNER="dufangshi"
DEFAULT_REPO="nanobot"
DEFAULT_BRANCH="prod"
DEFAULT_DEPLOY_PATH="/opt/nanobot"

if [ "${EUID}" -eq 0 ]; then
  DEFAULT_DEPLOY_USER="root"
else
  DEFAULT_DEPLOY_USER="${USER}"
fi

DEPLOY_USER="${DEFAULT_DEPLOY_USER}"
DEPLOY_PATH="${DEFAULT_DEPLOY_PATH}"
GITHUB_OWNER="${DEFAULT_OWNER}"
GITHUB_REPO="${DEFAULT_REPO}"
DEPLOY_BRANCH="${DEFAULT_BRANCH}"
DEPLOY_PORT="22"

echo "====================================================================="
echo "  Nanobot - GitHub Actions Deployment Setup"
echo "====================================================================="
echo
echo "This script prepares a Linux server for GitHub Actions based deployment."
echo "It will:"
echo "  - install Docker, Docker Compose, Git, and SSH tools"
echo "  - generate one SSH key for GitHub Actions -> server"
echo "  - generate one SSH key for server -> GitHub repo pulls"
echo "  - initialize the deployment directory"
echo "  - create ~/.nanobot for persistent config and workspace"
echo

read -r -p "Deploy user [${DEFAULT_DEPLOY_USER}]: " input_deploy_user
if [ -n "${input_deploy_user}" ]; then
  DEPLOY_USER="${input_deploy_user}"
fi

read -r -p "GitHub owner [${DEFAULT_OWNER}]: " input_owner
if [ -n "${input_owner}" ]; then
  GITHUB_OWNER="${input_owner}"
fi

read -r -p "GitHub repo [${DEFAULT_REPO}]: " input_repo
if [ -n "${input_repo}" ]; then
  GITHUB_REPO="${input_repo}"
fi

read -r -p "Deploy branch [${DEFAULT_BRANCH}]: " input_branch
if [ -n "${input_branch}" ]; then
  DEPLOY_BRANCH="${input_branch}"
fi

read -r -p "Deploy path [${DEFAULT_DEPLOY_PATH}]: " input_deploy_path
if [ -n "${input_deploy_path}" ]; then
  DEPLOY_PATH="${input_deploy_path}"
fi

read -r -p "SSH port for GitHub Actions [22]: " input_deploy_port
if [ -n "${input_deploy_port}" ]; then
  DEPLOY_PORT="${input_deploy_port}"
fi

if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
  echo "Deploy user '${DEPLOY_USER}' does not exist."
  exit 1
fi

DEPLOY_HOME="$(getent passwd "${DEPLOY_USER}" | cut -d: -f6)"
if [ -z "${DEPLOY_HOME}" ]; then
  echo "Failed to resolve home directory for user '${DEPLOY_USER}'."
  exit 1
fi

if [ "${EUID}" -ne 0 ]; then
  echo "This script must run as root or via sudo."
  exit 1
fi

run_as_deploy_user() {
  if [ "${DEPLOY_USER}" = "root" ]; then
    bash -lc "$1"
  else
    sudo -u "${DEPLOY_USER}" -H bash -lc "$1"
  fi
}

SERVER_IP="$(curl -4 -fsSL ifconfig.me || true)"

echo
echo "====================================================================="
echo "  Starting setup in 3 seconds. Press Ctrl+C to cancel."
echo "====================================================================="
sleep 3

echo
echo ">>> [1/5] Installing dependencies..."
apt-get update -y
apt-get install -y ca-certificates curl gnupg git openssh-client openssl

if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
    tee /etc/apt/sources.list.d/docker.list >/dev/null
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable docker >/dev/null 2>&1 || true
systemctl start docker >/dev/null 2>&1 || true

if [ "${DEPLOY_USER}" != "root" ]; then
  usermod -aG docker "${DEPLOY_USER}" || true
fi

echo
echo ">>> [2/5] Generating SSH keys..."
SSH_DIR="${DEPLOY_HOME}/.ssh"
install -d -m 700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${SSH_DIR}"

ACTIONS_KEY_FILE="${SSH_DIR}/id_ed25519_actions_deploy"
if [ ! -f "${ACTIONS_KEY_FILE}" ]; then
  run_as_deploy_user "ssh-keygen -t ed25519 -C 'actions-deploy-${GITHUB_REPO}' -f '${ACTIONS_KEY_FILE}' -N '' -q"
fi

AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"
touch "${AUTHORIZED_KEYS}"
chmod 600 "${AUTHORIZED_KEYS}"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${AUTHORIZED_KEYS}"
if ! grep -qxF "$(cat "${ACTIONS_KEY_FILE}.pub")" "${AUTHORIZED_KEYS}"; then
  cat "${ACTIONS_KEY_FILE}.pub" >> "${AUTHORIZED_KEYS}"
fi

REPO_KEY_FILE="${SSH_DIR}/id_ed25519_repo_pull"
if [ ! -f "${REPO_KEY_FILE}" ]; then
  run_as_deploy_user "ssh-keygen -t ed25519 -C 'repo-pull-${GITHUB_OWNER}-${GITHUB_REPO}' -f '${REPO_KEY_FILE}' -N '' -q"
fi

KNOWN_HOSTS="${SSH_DIR}/known_hosts"
touch "${KNOWN_HOSTS}"
chmod 644 "${KNOWN_HOSTS}"
chown "${DEPLOY_USER}:${DEPLOY_USER}" "${KNOWN_HOSTS}"
if ! grep -q "github.com" "${KNOWN_HOSTS}"; then
  ssh-keyscan -H github.com >> "${KNOWN_HOSTS}" 2>/dev/null || true
fi

SSH_CONFIG="${SSH_DIR}/config"
if [ ! -f "${SSH_CONFIG}" ]; then
  touch "${SSH_CONFIG}"
  chmod 600 "${SSH_CONFIG}"
  chown "${DEPLOY_USER}:${DEPLOY_USER}" "${SSH_CONFIG}"
fi

if ! grep -q "IdentityFile ${REPO_KEY_FILE}" "${SSH_CONFIG}"; then
  cat >> "${SSH_CONFIG}" <<EOF

Host github.com
  HostName github.com
  User git
  IdentityFile ${REPO_KEY_FILE}
  IdentitiesOnly yes
EOF
fi

echo
echo ">>> [3/5] Preparing deployment directory..."
mkdir -p "${DEPLOY_PATH}"
chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${DEPLOY_PATH}"

run_as_deploy_user "git config --global --add safe.directory '${DEPLOY_PATH}'"

if [ ! -d "${DEPLOY_PATH}/.git" ]; then
  run_as_deploy_user "cd '${DEPLOY_PATH}' && git init -q && git remote add origin 'git@github.com:${GITHUB_OWNER}/${GITHUB_REPO}.git'"
else
  run_as_deploy_user "cd '${DEPLOY_PATH}' && git remote set-url origin 'git@github.com:${GITHUB_OWNER}/${GITHUB_REPO}.git'"
fi

echo
echo ">>> [4/5] Preparing persistent nanobot data..."
NANOBOT_HOME="${DEPLOY_HOME}/.nanobot"
install -d -m 755 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${NANOBOT_HOME}"
install -d -m 755 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "${NANOBOT_HOME}/workspace"

echo
echo ">>> [5/5] Checking server-side git access..."
if run_as_deploy_user "GIT_SSH_COMMAND='ssh -o BatchMode=yes' git ls-remote 'git@github.com:${GITHUB_OWNER}/${GITHUB_REPO}.git' >/dev/null 2>&1"; then
  echo ">>> GitHub deploy key is already authorized."
else
  echo ">>> GitHub deploy key is not authorized yet. This is expected before you add it in GitHub."
fi

echo
echo "====================================================================="
echo "                           SETUP COMPLETE                            "
echo "====================================================================="
echo
echo "1. GitHub repository settings -> Deploy keys -> Add deploy key"
echo "   Title: Server Git Pull"
echo "   Allow write access: OFF"
echo "   Key:"
cat "${REPO_KEY_FILE}.pub"
echo
echo "---------------------------------------------------------------------"
echo
echo "2. GitHub repository settings -> Secrets and variables -> Actions"
echo "   Add these repository secrets:"
echo
echo "   DEPLOY_HOST"
echo "   ${SERVER_IP:-<your-server-ip>}"
echo
echo "   DEPLOY_PORT"
echo "   ${DEPLOY_PORT}"
echo
echo "   DEPLOY_USER"
echo "   ${DEPLOY_USER}"
echo
echo "   DEPLOY_PATH"
echo "   ${DEPLOY_PATH}"
echo
echo "   DEPLOY_SSH_KEY"
cat "${ACTIONS_KEY_FILE}"
echo
echo "---------------------------------------------------------------------"
echo
echo "3. Then trigger deployment:"
echo "   - push to '${DEPLOY_BRANCH}', or"
echo "   - GitHub -> Actions -> Deploy Nanobot -> Run workflow"
echo
echo "4. After first deployment, review server config at:"
echo "   ${NANOBOT_HOME}/config.json"
echo
echo "Raw repo URL for this script:"
echo "  https://raw.githubusercontent.com/${GITHUB_OWNER}/${GITHUB_REPO}/${DEPLOY_BRANCH}/deploy_setup.sh"
echo

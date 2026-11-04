#!/usr/bin/env bash
# Run interactively ON the CRM VPS after gh auth login. Never displays secrets.
set -euo pipefail
umask 077
REPOSITORY=abdylzhelil/CRM-bot-tg
CRM_PATH=$(pwd -P)
[[ -f "$CRM_PATH/docker/autodeploy.sh" && -f "$CRM_PATH/.env" ]] || { echo 'Run from the CRM working directory'; exit 1; }
[[ "$(git branch --show-current)" = ccr-a042c0a9-cjx9fv ]] || { echo 'Unexpected CRM branch'; exit 1; }
command -v gh >/dev/null
gh auth status >/dev/null 2>&1 || { echo 'First run: gh auth login --hostname github.com --git-protocol https --web'; exit 1; }
# Disable before touching credentials; never enables deployment on its own.
gh variable set CRM_AUTO_DEPLOY_ENABLED --repo "$REPOSITORY" --body false
read -r -p 'CRM server SSH IP or DNS (as in Termius): ' CRM_HOST
read -r -p 'SSH port [22]: ' CRM_PORT
CRM_PORT=${CRM_PORT:-22}
CRM_USER=$(id -un)
[[ "$CRM_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ && "$CRM_PORT" =~ ^[0-9]+$ ]] || exit 2
((CRM_PORT>=1 && CRM_PORT<=65535)) || exit 2
install -m 700 -d "$HOME/.ssh"
KEY="$HOME/.ssh/crm_actions_ed25519"
if [[ ! -e "$KEY" && ! -e "$KEY.pub" ]]; then
    ssh-keygen -q -t ed25519 -N '' -C crm-github-actions -f "$KEY"
fi
[[ -f "$KEY" && -f "$KEY.pub" ]] || { echo 'Incomplete CRM key pair; inspect manually'; exit 1; }
chmod 600 "$KEY"
# Check the pair without displaying either key.
[[ "$(ssh-keygen -y -f "$KEY")" = "$(awk '{print $1, $2}' "$KEY.pub")" ]] || { echo 'CRM key pair mismatch'; exit 1; }
touch "$HOME/.ssh/authorized_keys"
chmod 600 "$HOME/.ssh/authorized_keys"
PUB=$(cat "$KEY.pub")
if ! grep -Fq -- "$PUB" "$HOME/.ssh/authorized_keys"; then
    printf '\nrestrict %s\n' "$PUB" >> "$HOME/.ssh/authorized_keys"
fi
gh secret set CRM_DEPLOY_SSH_KEY --repo "$REPOSITORY" < "$KEY"
# Pin the local host's real public key; do not trust an unauthenticated ssh-keyscan.
awk '{print "*", $1, $2}' /etc/ssh/ssh_host_ed25519_key.pub | gh secret set CRM_DEPLOY_KNOWN_HOSTS --repo "$REPOSITORY"
printf '%s' "$CRM_HOST" | gh secret set CRM_DEPLOY_HOST --repo "$REPOSITORY"
printf '%s' "$CRM_USER" | gh secret set CRM_DEPLOY_USER --repo "$REPOSITORY"
printf '%s' "$CRM_PORT" | gh secret set CRM_DEPLOY_PORT --repo "$REPOSITORY"
gh variable set CRM_DEPLOY_PATH --repo "$REPOSITORY" --body "$CRM_PATH"
echo 'CRM credentials uploaded directly. CRM_AUTO_DEPLOY_ENABLED remains false.'
echo 'No private keys or tokens need to be copied or sent in chat.'

#!/usr/bin/env bash
# CRM only. Invoked with the exact SHA whose two CI jobs passed.
set -Eeuo pipefail
umask 077
TARGET=${1:?Expected commit SHA}
REPO=${2:?Expected absolute repository path}
BRANCH=ccr-a042c0a9-cjx9fv
[[ "$TARGET" =~ ^[0-9a-f]{40}$ && "$REPO" = /* ]] || exit 2
cd -- "$REPO"
for program in git docker python3 flock tar timeout sha256sum cmp; do command -v "$program" >/dev/null; done
exec 9>"$(git rev-parse --git-path crm-deploy.lock)"
flock -n 9 || { echo 'Another CRM deployment is running' >&2; exit 1; }
[[ "$(git branch --show-current)" = "$BRANCH" ]] || { echo 'Unexpected CRM branch' >&2; exit 1; }
clean() { [[ -z "$(git status --porcelain --untracked-files=all)" ]]; }
clean || { echo 'Local changes found; preserve and review them manually' >&2; exit 1; }
[[ -f .env ]] || { echo 'Missing CRM .env' >&2; exit 1; }
latest() {
    git fetch --quiet origin "$BRANCH"
    [[ "$(git rev-parse FETCH_HEAD)" = "$TARGET" ]]
}
latest || { echo 'Skipped: obsolete commit'; exit 0; }
PREVIOUS=$(git rev-parse HEAD)
ENV_STATE=$(sha256sum .env)
git merge-base --is-ancestor "$PREVIOUS" "$TARGET" || { echo 'Not a fast-forward update' >&2; exit 1; }
[[ "$PREVIOUS" != "$TARGET" ]] || { echo 'Already installed; no switch performed'; exit 0; }
# CRM does not use Alembic: create_all + migrate + seed run at EVERY startup.
# Deliberately conservative: even data migrations/seed changes need manual review.
if [[ -n "$(git diff --name-only "$PREVIOUS" "$TARGET" -- app/models.py app/migrate.py app/db.py app/seed.py app/dbcopy.py migrations alembic 'docker-compose*' 'compose*' .env)" ]]; then
    echo 'Schema/startup data or Compose configuration changed; reviewed manual deployment required' >&2
    exit 1
fi
# Catch explicit DDL introduced outside the standard schema files as well.
if git diff --unified=0 "$PREVIOUS" "$TARGET" -- 'app/*.py' 'app/**/*.py' | python3 -c 'import re,sys; s=sys.stdin.read(); sys.exit(0 if re.search(r"^\+[^+].*(?:CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|create_all|drop_all|upgrade_schema)",s,re.I|re.M) else 1)'; then
    echo 'Possible database structure change outside standard files; manual review required' >&2
    exit 1
fi
CURRENT=$(docker compose ps -q bot)
[[ -n "$CURRENT" && "$CURRENT" != *$'\n'* ]] || { echo 'Expected one running CRM bot' >&2; exit 1; }
[[ "$(docker inspect -f '{{.State.Running}}' "$CURRENT")" = true ]] || exit 1
PROJECT=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CURRENT")
[[ -n "$PROJECT" && "$PROJECT" != 'tg-group-bot' ]] || { echo 'Invalid CRM project' >&2; exit 1; }
OLD_IMAGE=$(docker inspect -f '{{.Image}}' "$CURRENT")
ROLLBACK_TAG="crm-rollback:$PREVIOUS"
docker image tag "$OLD_IMAGE" "$ROLLBACK_TAG"
STATE=$(git rev-parse --git-path crm-autodeploy)
mkdir -p -- "$STATE"
STATE=$(cd "$STATE" && pwd)
RELEASE=$(mktemp -d "$STATE/release.XXXXXXXX")
TOUCHED=0
old_compose() { docker compose --project-directory "$REPO" --project-name "$PROJECT" -f "$RELEASE/old.json" "$@"; }
new_compose() { docker compose --project-directory "$REPO" --project-name "$PROJECT" -f "$RELEASE/new.json" "$@"; }
rollback_compose() { docker compose --project-directory "$REPO" --project-name "$PROJECT" -f "$RELEASE/rollback.json" "$@"; }
source_unchanged() {
    [[ "$(git rev-parse HEAD)" = "$PREVIOUS" && "$(git branch --show-current)" = "$BRANCH" ]] && clean &&
        [[ "$(sha256sum .env)" = "$ENV_STATE" ]] &&
        docker compose config --format json > "$RELEASE/check.json" &&
        cmp -s "$RELEASE/old.json" "$RELEASE/check.json"
}
healthy() {
    local expected=$1 successes=0 container image
    for ((attempt=0; attempt<36; attempt++)); do
        container=$(old_compose ps -q bot 2>/dev/null) || container=''
        image=''
        if [[ -n "$container" ]]; then image=$(docker inspect -f '{{.Image}}' "$container" 2>/dev/null) || image=''; fi
        if [[ "$image" = "$expected" ]] && timeout 12 docker exec "$container" python -c 'import json,urllib.request; from app import config; assert config.WEBAPP_URL; r=urllib.request.urlopen("http://127.0.0.1:%s/healthz" % config.WEBAPP_PORT,timeout=4); assert r.status==200 and json.load(r).get("ok") is True' >/dev/null 2>&1; then
            successes=$((successes+1))
            if ((successes >= 3)); then return 0; fi
        else
            successes=0
        fi
        sleep 5
    done
    return 1
}
finish() {
    local status=$?
    trap - EXIT
    if ((status != 0 && TOUCHED == 1)); then
        echo 'CRM deployment failed; restoring previous image. Database is NOT restored.' >&2
        if rollback_compose up -d --no-build --no-deps bot >/dev/null 2>&1 && healthy "$OLD_IMAGE"; then
            echo "Previous CRM image restored: $PREVIOUS" >&2
        else
            echo 'ROLLBACK FAILED: manual intervention required; backup retained' >&2
        fi
    fi
    rm -rf -- "$RELEASE"
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
# Resolve the ACTIVE configuration, including COMPOSE_FILE in .env and overrides.
# It contains secrets: keep private, never print it, remove on exit.
docker compose config --format json > "$RELEASE/old.json"
python3 - "$RELEASE" "$PROJECT" "$REPO" "$TARGET" "$ROLLBACK_TAG" <<'PY'
import copy,json,pathlib,sys
release,project,repo,target,rollback=sys.argv[1:]
p=pathlib.Path(release)
cfg=json.loads((p/'old.json').read_text())
assert cfg['name']==project and set(cfg['services'])=={'bot'}, 'Only the isolated CRM bot service is supported'
bot=cfg['services']['bot']
assert bot.get('working_dir','/srv')=='/srv'
assert any(v.get('type')=='bind' and v.get('target')=='/srv/data' and pathlib.Path(v['source']).resolve()==pathlib.Path(repo,'data').resolve() for v in bot.get('volumes',[])), 'Unexpected CRM data mount'
assert all(v.get('external') for v in cfg.get('networks',{}).values() if v.get('name') in ('tgbots','tg-group-bot_default')), 'Shared networks must remain external'
old=copy.deepcopy(cfg)
old['services']['bot'].pop('build',None)
old['services']['bot']['image']=rollback
(p/'rollback.json').write_text(json.dumps(old))
bot['image']='crm-bot:'+target
bot['build']={'context':str(p/'source'),'dockerfile':'Dockerfile'}
(p/'new.json').write_text(json.dumps(cfg))
PY
healthy "$OLD_IMAGE" || { echo 'Current CRM /healthz baseline failed; no update attempted' >&2; exit 1; }
mkdir -p "$RELEASE/source"
git archive "$TARGET" | tar -x -C "$RELEASE/source"
# Use the running image's config; no credentials/DB URL leave the container.
BACKUP="before_auto_${TARGET:0:12}_$(date -u +%Y%m%d_%H%M%S)_$$"
echo "Creating and verifying CRM backup: $BACKUP.db"
timeout 180 docker exec -i "$CURRENT" python - "$BACKUP" <<'PY'
import os,pathlib,sqlite3,sys
from app import config
from sqlalchemy.engine import make_url
os.umask(0o077)
url=make_url(config.DATABASE_URL)
assert url.get_backend_name()=='sqlite', 'PostgreSQL requires a separately reviewed native backup setup; deployment stopped'
src=pathlib.Path(url.database).resolve()
assert src.is_file() and src.is_relative_to(pathlib.Path('/srv/data')), 'Unexpected SQLite location'
folder=pathlib.Path('/srv/data/backups/autodeploy')
folder.mkdir(parents=True,exist_ok=True,mode=0o700)
dest=folder/(sys.argv[1]+'.db')
with sqlite3.connect(src.as_uri()+'?mode=ro',uri=True,timeout=30) as source, sqlite3.connect(dest) as target:
    source.backup(target,pages=256,sleep=0.05)
    assert target.execute('PRAGMA integrity_check').fetchall()==[('ok',)], 'Backup integrity check failed'
    tables={r[0] for r in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'users','payments','settings'} <= tables, 'Backup missing core CRM tables'
os.chmod(dest,0o600)
PY
BACKUP_PATH="$REPO/data/backups/autodeploy/$BACKUP.db"
[[ -s "$BACKUP_PATH" ]] || { echo 'Backup is not on persistent CRM storage' >&2; exit 1; }
cp -- .env "$BACKUP_PATH.env"
cp -- "$RELEASE/old.json" "$BACKUP_PATH.compose.json"
printf '%s\n' "$PREVIOUS" > "$BACKUP_PATH.commit"
printf '%s\n' "$OLD_IMAGE" > "$BACKUP_PATH.image"
sha256sum "$BACKUP_PATH" > "$BACKUP_PATH.sha256"
# Compile while the old bot is still running; no new Telegram polling process.
new_compose build bot </dev/null
NEW_IMAGE=$(docker image inspect -f '{{.Id}}' "crm-bot:$TARGET")
latest || { echo 'Skipped: newer commit arrived during build'; exit 0; }
source_unchanged || { echo 'Local configuration or source changed during build; stopped' >&2; exit 1; }
echo "Switching CRM: $PREVIOUS -> $TARGET"
TOUCHED=1
new_compose up -d --no-build --no-deps bot
healthy "$NEW_IMAGE" || { echo 'New CRM failed /healthz checks' >&2; exit 1; }
source_unchanged || { echo 'Local configuration or source changed during startup; stopped' >&2; exit 1; }
git merge --ff-only "$TARGET" >/dev/null
TOUCHED=0
echo "CRM deployment healthy: $TARGET"

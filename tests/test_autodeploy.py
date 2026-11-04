"""Exercise the real deployment shell with real Git repositories and a fake Docker daemon.

No server, Telegram or production database is touched. The mock records every Docker
operation and lets tests inject failures before and after the service switch.
"""
import json
import fcntl
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'docker/autodeploy.sh'
BRANCH = 'ccr-a042c0a9-cjx9fv'


def git(folder, *args):
    return subprocess.check_output(['git', '-C', str(folder), *args], text=True).strip()


@pytest.fixture
def deployment(tmp_path):
    upstream, repo, tools = (tmp_path / n for n in ('origin', 'crm', 'bin'))
    upstream.mkdir()
    tools.mkdir()
    git(upstream, 'init', '-b', BRANCH)
    git(upstream, 'config', 'user.email', 'test@example.com')
    git(upstream, 'config', 'user.name', 'Test')
    (upstream / '.gitignore').write_text('.env\ndata/\n')
    (upstream / 'README.md').write_text('old')
    (upstream / 'app').mkdir()
    (upstream / 'app/models.py').write_text('old schema')
    git(upstream, 'add', '.')
    git(upstream, 'commit', '-m', 'baseline')
    subprocess.run(['git', 'clone', '-q', str(upstream), str(repo)], check=True)
    old = git(repo, 'rev-parse', 'HEAD')
    (repo / '.env').write_text('DO_NOT_LOSE=local\n')
    cfg = {'name': 'crm-bot-tg', 'services': {'bot': {
        'build': {'context': str(repo)}, 'environment': {'SECRET': 'do-not-print'},
        'volumes': [{'type': 'bind', 'source': str(repo / 'data'), 'target': '/srv/data'}],
        'networks': {'default': {}, 'tgbots': {}, 'webproxy': {}}}},
        'networks': {'default': {}, 'tgbots': {'external': True, 'name': 'tgbots'},
                     'webproxy': {'external': True, 'name': 'tg-group-bot_default'}}}
    (tmp_path / 'config.json').write_text(json.dumps(cfg))
    (tmp_path / 'image').write_text('old-image')
    fake = tools / 'docker'
    fake.write_text('''#!/usr/bin/env python3
import json,os,pathlib,sys
p=pathlib.Path(os.environ['FAKE_STATE']); a=sys.argv[1:]
with (p/'calls').open('a') as f: f.write(json.dumps(a)+'\\n')
mode=os.environ.get('FAKE_MODE','')
if a[0]=='compose':
    if 'config' in a: print((p/'config.json').read_text())
    elif 'ps' in a: print('crm-container')
    elif 'build' in a:
        if mode=='build-fail': sys.exit(1)
        if mode=='local-during-build': (p/'crm/.env').write_text('CHANGED=1')
        if mode=='stale-during-build':
            import subprocess
            subprocess.run(['git','-C',str(p/'origin'),'commit','--allow-empty','-m','newer'],check=True,stdout=subprocess.DEVNULL)
    elif 'up' in a:
        config=json.loads(pathlib.Path(a[a.index('-f')+1]).read_text())
        image=config['services']['bot']['image']
        if image.startswith('crm-rollback:'):
            if mode=='rollback-fail': sys.exit(1)
            (p/'image').write_text('sha256:recovery-image' if mode.startswith('missing-image') else 'old-image')
        else:
            (p/'image').write_text('new-image')
            if mode=='switch-fail': sys.exit(1)
    else: sys.exit(3)
elif a[0]=='inspect':
    fmt=a[a.index('-f')+1]
    print('true' if 'State.Running' in fmt else 'crm-bot-tg' if 'Labels' in fmt else (p/'image').read_text())
elif a[:2]==['image','inspect']:
    if mode.startswith('missing-image') and a[-1]=='old-image': sys.exit(1)
    print('new-image')
elif a[:2]==['image','tag']: pass
elif a[0]=='commit':
    assert '--pause=false' in a
    print('sha256:recovery-image')
elif a[0]=='exec':
    if '-i' in a:
        sys.stdin.read()
        if mode=='backup-fail': sys.exit(1)
        dest=p/'crm/data/backups/autodeploy'/(a[-1]+'.db')
        dest.parent.mkdir(parents=True,exist_ok=True); dest.write_bytes(b'verified SQLite mock')
    elif (p/'image').read_text()=='new-image' and mode in ('health-fail','rollback-fail','missing-image-health-fail'): sys.exit(1)
else: sys.exit(4)
''')
    fake.chmod(0o755)
    (tools / 'sleep').write_text('#!/bin/sh\nexit 0\n')
    (tools / 'sleep').chmod(0o755)
    env = {**os.environ, 'PATH': str(tools)+os.pathsep+os.environ['PATH'], 'FAKE_STATE': str(tmp_path)}

    def run(mode='', change='README.md', dirty=False):
        (upstream / change).write_text('new revision')
        git(upstream, 'add', '.')
        git(upstream, 'commit', '-m', 'candidate')
        target=git(upstream, 'rev-parse', 'HEAD')
        if dirty:
            (repo / 'README.md').write_text('owner changes')
        result=subprocess.run(['bash', str(SCRIPT), target, str(repo)], env={**env, 'FAKE_MODE': mode}, text=True, capture_output=True, timeout=20)
        calls=[json.loads(l) for l in (tmp_path/'calls').read_text().splitlines()] if (tmp_path/'calls').exists() else []
        assert 'do-not-print' not in result.stdout + result.stderr
        return result, calls, target

    return run, tmp_path, repo, old


def test_success_preserves_env_and_external_networks(deployment):
    run, state, repo, old=deployment
    result, calls, target=run()
    assert result.returncode==0, result.stderr
    assert git(repo,'rev-parse','HEAD')==target
    assert (repo/'.env').read_text()=='DO_NOT_LOSE=local\n'
    backups=list((repo/'data/backups/autodeploy').glob('*.db'))
    assert len(backups)==1
    assert backups[0].with_suffix('.db.commit').read_text().strip()==old
    assert any('build' in c for c in calls)
    switches=[c for c in calls if 'up' in c]
    assert len(switches)==1
    assert all(c[-1]=='bot' and '--no-deps' in c for c in switches)
    assert not list((repo/'.git/crm-autodeploy').glob('release.*'))


@pytest.mark.parametrize('mode',['health-fail','switch-fail'])
def test_failed_new_image_rolls_back_but_job_fails(deployment, mode):
    run,state,repo,old=deployment
    result,calls,_=run(mode)
    assert result.returncode!=0
    assert 'Previous CRM image restored' in result.stderr
    assert (state/'image').read_text()=='old-image'
    assert git(repo,'rev-parse','HEAD')==old
    assert len([c for c in calls if 'up' in c])==2


def test_rollback_failure_is_explicit(deployment):
    result,_,_=deployment[0]('rollback-fail')
    assert result.returncode!=0 and 'ROLLBACK FAILED' in result.stderr


@pytest.mark.parametrize('mode',['missing-image','missing-image-health-fail'])
def test_missing_original_image_is_preserved_without_pausing(deployment, mode):
    run,state,repo,old=deployment
    result,calls,target=run(mode)
    assert any(c[0]=='commit' and '--pause=false' in c for c in calls)
    if mode.endswith('health-fail'):
        assert result.returncode!=0
        assert 'Previous CRM image restored' in result.stderr
        assert (state/'image').read_text()=='sha256:recovery-image'
        assert git(repo,'rev-parse','HEAD')==old
    else:
        assert result.returncode==0, result.stderr
        assert git(repo,'rev-parse','HEAD')==target


@pytest.mark.parametrize('mode',['backup-fail','build-fail','stale-during-build','local-during-build'])
def test_pre_switch_failures_leave_running_bot(deployment, mode):
    run,state,repo,old=deployment
    result,calls,_=run(mode)
    assert (state/'image').read_text()=='old-image'
    assert not any('up' in c for c in calls)
    assert git(repo,'rev-parse','HEAD')==old
    assert result.returncode==(0 if mode=='stale-during-build' else 1)


def test_schema_change_is_blocked_before_docker(deployment):
    result,calls,_=deployment[0](change='app/models.py')
    assert result.returncode!=0 and not calls
    assert 'manual deployment required' in result.stderr


def test_parallel_deployment_is_blocked(deployment):
    run,_,repo,_=deployment
    with (repo/'.git/crm-deploy.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result,calls,_=run()
    assert result.returncode!=0 and not calls
    assert 'Another CRM deployment' in result.stderr


def test_local_edits_are_preserved(deployment):
    run,state,repo,old=deployment
    result,calls,_=run(dirty=True)
    assert result.returncode!=0 and not calls
    assert (repo/'README.md').read_text()=='owner changes'
    assert git(repo,'rev-parse','HEAD')==old


def test_real_sqlite_wal_backup_is_consistent(tmp_path):
    # Run the exact embedded container code with only its filesystem root relocated.
    source=SCRIPT.read_text().split('timeout 180 docker exec -i "$CURRENT" python - "$BACKUP" <<\'PY\'\n',1)[1].split('\nPY\n',1)[0]
    data=tmp_path/'data'
    data.mkdir()
    app=tmp_path/'app'
    app.mkdir()
    (app/'__init__.py').write_text('')
    (app/'config.py').write_text(f'DATABASE_URL = "sqlite+aiosqlite:///{data}/crm.db"\n')
    db=sqlite3.connect(data/'crm.db')
    try:
        db.execute('PRAGMA journal_mode=WAL')
        for table in ('users','payments','settings'):
            db.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT)')
            db.execute(f'INSERT INTO {table} (value) VALUES (?)', ('must survive WAL backup',))
        db.commit()
        code=source.replace('/srv/data', str(data))
        result=subprocess.run([os.sys.executable,'-','test'],input=code,text=True,capture_output=True,cwd=tmp_path,timeout=15)
        assert result.returncode==0, result.stderr
        backup=data/'backups/autodeploy/test.db'
        with sqlite3.connect(backup) as saved:
            assert saved.execute('PRAGMA integrity_check').fetchone()==('ok',)
            assert saved.execute('SELECT value FROM payments').fetchone()==('must survive WAL backup',)
        assert backup.stat().st_mode & 0o777 == 0o600
    finally:
        db.close()

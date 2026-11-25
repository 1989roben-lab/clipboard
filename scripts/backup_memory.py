#!/usr/bin/env python3
"""Paired offline Memory database + attachment backups. Run on the Docker host."""
import argparse
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time

def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)

def backup(destination):
    os.umask(0o077)
    root=Path(destination);root.mkdir(parents=True,exist_ok=True)
    with (root/'backup.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        working=Path(tempfile.mkdtemp(prefix='.partial-',dir=root))
        running=json.loads(subprocess.check_output(['docker','inspect','lan-clipboard-8016']))[0]['State']['Running']
        try:
            if running:run('docker','stop','-t','30','lan-clipboard-8016',stdout=subprocess.DEVNULL)
            with (working/'memory.archive.gz').open('wb') as f:
                run('docker','exec','mongodb','sh','-c',
                    'exec mongodump --username "$MONGO_INITDB_ROOT_USERNAME" --password "$MONGO_INITDB_ROOT_PASSWORD" --authenticationDatabase admin --db memory --dumpDbUsersAndRoles --archive --gzip',
                    stdout=f,stderr=subprocess.PIPE)
            with (working/'uploads.tar.gz').open('wb') as f:
                run('docker','run','--rm','--network','none','-v','lan-clipboard-8016-data:/legacy:ro','python:3.13-alpine',
                    'tar','czf','-','-C','/legacy','uploads',stdout=f)
            with tarfile.open(working/'uploads.tar.gz') as tar: tar.getmembers()
            checks={}
            for name in ('memory.archive.gz','uploads.tar.gz'):
                h=hashlib.sha256()
                with (working/name).open('rb') as f:
                    for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
                checks[name]={'sha256':h.hexdigest(),'bytes':(working/name).stat().st_size}
            (working/'manifest.json').write_text(json.dumps({'created_at':datetime.now().isoformat(),'complete':True,'files':checks},indent=2))
            final=root/datetime.now().strftime('%Y%m%d-%H%M%S');working.rename(final)
            cutoff=time.time()-7*86400
            for p in root.iterdir():
                if p.is_dir() and len(p.name)==15 and p.name.replace('-','').isdigit() and (p/'manifest.json').is_file() and p.stat().st_mtime<cutoff:
                    shutil.rmtree(p)
            print('Paired Memory backup complete:', final,flush=True)
        finally:
            if running:run('docker','start','lan-clipboard-8016',stdout=subprocess.DEVNULL)
            if working.exists():shutil.rmtree(working)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--backup-dir',default='/home/aa/Vibecoding/shanghai-migration/backups/memory-daily');args=p.parse_args()
    backup(args.backup_dir)

#!/usr/bin/env python3
"""Install the pinned private skills before starting ZeroClaw."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil

spec = importlib.util.spec_from_file_location('private_context', Path(__file__).with_name('langfuse-private.py'))
private_context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(private_context)
FILES = {'SKILL.md': 'memini/SKILL.md', 'caveman/SKILL.md': 'caveman/SKILL.md'}


def validate(bundle, version):
    if bundle.get('name') != 'zeroclaw-policy' or bundle.get('version') != version:
        raise ValueError('Unexpected private skill version')
    files = bundle.get('files', [])
    if len(files) != len(FILES) or {f.get('path') for f in files} != set(FILES):
        raise ValueError('Unexpected private skill files')
    for file in files:
        content = file.get('content')
        if not isinstance(content, str) or not 0 < len(content.encode()) <= 32000:
            raise ValueError('Invalid private skill content')
        digest = base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
        if digest != file.get('sha256Hash') or '\nalways: true\n' not in content:
            raise ValueError('Private skill checksum or prompt policy mismatch')
    return files


def install(root, source, version):
    cache = root / 'private-skills.json'
    try:
        bundle = private_context.request(f'/api/public/skills/zeroclaw-policy?version={version}&includeContent=true')
    except private_context.Unavailable:
        bundle = json.loads(cache.read_text())
    files = validate(bundle, version)
    target = root / 'shared/skills/baseline'
    for file in files:
        path = target / FILES[file['path']]
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(file['content'])
        path.chmod(0o600)
    cache.write_text(json.dumps(bundle))
    cache.chmod(0o600)
    shutil.copyfile(source, root / 'config.toml')


if __name__ == '__main__':
    root = Path('/bootstrap/agent/.zeroclaw')
    root.mkdir(parents=True, exist_ok=True)
    Path('/bootstrap/automation/state').mkdir(parents=True, exist_ok=True)
    install(root, Path('/config/config.toml'), int(os.environ['ZEROCLAW_SKILL_VERSION']))

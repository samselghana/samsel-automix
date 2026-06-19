from __future__ import annotations

import json
from pathlib import Path

MIXER_IMPORT_ROOT = Path('./mixer_import/default')


def load_new_manifests() -> list[dict]:
    manifests = []
    for path in MIXER_IMPORT_ROOT.glob('job_*_manifest.json'):
        manifests.append(json.loads(path.read_text(encoding='utf-8')))
    return manifests


if __name__ == '__main__':
    for manifest in load_new_manifests():
        print(f"Import job #{manifest['job_id']}")
        for item in manifest['files']:
            print(' -', item)

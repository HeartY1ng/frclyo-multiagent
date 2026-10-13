#!/usr/bin/env python3
"""离线钥匙串替身：只记录服务名，不读写真实钥匙串或任何密码。"""
import json
import os
from pathlib import Path
import sys
path = Path(os.environ['FRCLYO_CONFIG']).parent / 'fake-keychain.json'
names = json.loads(path.read_text()) if path.exists() else []
service = sys.argv[sys.argv.index('-s') + 1]
command = sys.argv[1]
if command == 'find-generic-password':
    if service not in names:
        sys.exit(44)
    if '-w' in sys.argv:
        print('OFFLINE-SENTINEL')
elif command == 'add-generic-password':
    if service in names:
        sys.exit(45)
    names.append(service)
    path.write_text(json.dumps(names))
elif command == 'delete-generic-password':
    if service not in names:
        sys.exit(44)
    names.remove(service)
    path.write_text(json.dumps(names))
else:
    sys.exit(1)

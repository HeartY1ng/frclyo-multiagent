#!/usr/bin/env python3
"""统一执行三组必过离线测试；缺组、零测试或跳过都按失败处理。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def main():
    """各组单独进程隔离模块缓存；主机 TCP/真实模型验收有独立入口。"""
    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) == 3 and sys.argv[1] == '--suite':
        suite = unittest.defaultTestLoader.discover(sys.argv[2], pattern='test_*.py')
        count = suite.countTestCases()
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if count and result.wasSuccessful() and not result.skipped else 1
    failed = False
    with tempfile.TemporaryDirectory(prefix='.offline-tests-', dir=str(root)) as temporary:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', FRCLYO_LAUNCHD_DRY_RUN='1',
                   TMPDIR=temporary, TMP=temporary, TEMP=temporary)
        # 各测试自行建立假 Codex 目录，不能继承运行者的真实账号路径。
        env.pop('CODEX_HOME', None)
        for group in ('bin/tests', 'board', 'tests'):
            print('必过离线测试：' + group, flush=True)
            result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                     '--suite', str(root / group)], cwd=root, env=env)
            failed = failed or result.returncode != 0
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())

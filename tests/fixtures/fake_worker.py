#!/usr/bin/env python3
# 只供离线测试：遵循旧执行脚本的位置参数、输入目录及回执格式。
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bin'))
from frclyo import config

# 独立写各自轮次，材料注入与真实工人一致。
task, round_id, card, model, level, timeout = sys.argv[1:]
work = Path(config.path_of('work_dir'))
directory = Path(config.path_of('deliver_dir')) / task / round_id
directory.mkdir(parents=True, exist_ok=False)
shutil.copytree(os.environ['WORKER_INPUT_DIR'], directory / '输入')
text = Path(card).read_text(encoding='utf-8')
# 通用模型拒绝注入：每个失败仍有独立事件与回执，CLI 版本由测试命令控制。
worker = 'opencode' if Path(__file__).name.startswith('opencode') else 'codex'
if model in os.environ.get('FAKE_UNUSABLE', '').split(','):
    message = json.dumps({'status': 400, 'error': {'message':
                         "The '%s' model is not supported when using this CLI with this account." % model}})
    (directory / 'events.jsonl').write_text(json.dumps({'type': 'turn.failed', 'error': {'message': message}}) + '\n')
    (directory / '回执.json').write_text(json.dumps({'工人': worker, '模型': model,
        '任务编号': task, '轮次编号': round_id, '退出码': 1, '状态': '失败', '结果sha256': None}))
    sys.exit(1)
# 校准使用测试专属参考实现，生产派活材料不包含答案。
if '# 校准任务 ' in text:
    import runpy
    question = text.split('# 校准任务 ', 1)[1].split()[0]
    answers = runpy.run_path(str(ROOT / 'tests/test_calibrate.py'))['SOLUTIONS']
    (directory / 'solve.py').write_text(answers[question], encoding='utf-8')
    bad = os.environ.get('FAKE_CALIBRATE_BAD', '')
    if bad == 'grade':
        (directory / 'solve.py').write_text('# 故意交付错误答案\n')
else:
    role = 'normalize' if '# 子任务 A' in text else 'summarize'
    fixture = ROOT / 'tests/fixtures'
    script = (fixture / 'csv_worker.py').read_text(encoding='utf-8')
    bad = os.environ.get('FAKE_BAD', '')
    if bad == 'e2e' and role == 'summarize':
        script = script.replace('# E2E_MUTATION：故障注入只改测试复制品。',
                                "if 'e2e' in source.name: output = []")
    (directory / (role + '.py')).write_text(script, encoding='utf-8')
    tests = (fixture / 'worker_tests.py').read_text(encoding='utf-8')
    if bad == 'tests' and role == 'summarize':
        tests = tests.replace('self.assertEqual(self.call', 'self.assertNotEqual(self.call')
    if bad == 'zero-tests':
        tests = '# 故障样例：没有测试\n'
    (directory / ('test_' + role + '.py')).write_text(tests, encoding='utf-8')

    # 两个假工人必须同时启动才能越过屏障，证明演示没有退化成串行。
    if os.environ.get('FAKE_REQUIRE_PARALLEL') == '1':
        with (work / 'fake-starts').open('a', encoding='utf-8') as stream:
            stream.write(task + '\n')
        deadline = time.monotonic() + 8
        while len((work / 'fake-starts').read_text().splitlines()) < 2:
            if time.monotonic() > deadline:
                sys.exit(42)
            time.sleep(0.05)
    source = directory / '输入' / ('raw.csv' if role == 'normalize' else 'normalized.expected.csv')
    sample = directory / ('normalized.sample.csv' if role == 'normalize' else 'summary.sample.csv')
    subprocess.run([sys.executable, str(directory / (role + '.py')), str(source), str(sample)], check=True)
    if bad == 'sample' and role == 'summarize':
        sample.write_bytes(b'wrong\n')

# 回执与结果分别生成；坏哈希和错误退出各有独立故障注入。
result = b'offline worker completed\n'
(directory / '结果.txt').write_bytes(result)
worker = 'opencode' if Path(__file__).name.startswith('opencode') else 'codex'
receipt = {'工人': worker, '模型': model, '任务编号': task, '轮次编号': round_id,
           '退出码': 0, '状态': '执行完成', '用量': None,
           '结果sha256': hashlib.sha256(result).hexdigest()}
if bad == 'receipt':
    receipt['结果sha256'] = 'broken'
if bad != 'missing-receipt':
    (directory / '回执.json').write_text(json.dumps(receipt, ensure_ascii=False), encoding='utf-8')
sys.exit(1 if bad == 'exit' else 0)

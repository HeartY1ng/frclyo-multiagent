#!/usr/bin/env python3
# 三关验收只使用固定材料和实际执行结果，不信任工人的口头结论。
import json
import os
from pathlib import Path
import signal
import subprocess
import sys


class CheckError(Exception):
    """验收失败的简短原因，会写入父子账本。"""


def execute(argv, cwd, data=None, timeout=30):
    """独立进程组控制超时；避免失败测试留下后台子进程。"""
    process = subprocess.Popen([str(v) for v in argv], cwd=str(cwd), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        out, err = process.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise CheckError('验收命令超时') from exc
    return process.returncode, out, err


def regular(directory, name):
    """产物必须是轮次内的普通文件，不能借符号链接引用其他轮次。"""
    path = directory / name
    if path.is_symlink() or not path.is_file() or path.resolve().parent != directory.resolve():
        raise CheckError('缺少普通产物文件：' + name)
    return path


def compare(actual, expected, label):
    """逐字节比较包含编码、引号、排序和最后一行换行。"""
    if actual != expected:
        raise CheckError(label + ' 与期望不一致')


def run_csv(directory, script, source, destination):
    """每次执行使用新的输出路径，避免旧产物冒充新结果。"""
    destination.unlink(missing_ok=True)
    rc, _, _ = execute([sys.executable, regular(directory, script), source, destination], directory)
    if rc or not destination.is_file() or destination.is_symlink():
        raise CheckError(script + ' 执行失败或未生成输出')
    return destination.read_bytes()


def demo_gates(a, b, samples, checks):
    """任何一关失败都抛出异常；调用方负责写入账本及保留证据。"""
    checks.mkdir(parents=True, exist_ok=False)
    normalized = (samples / 'normalized.expected.csv').read_bytes()
    summary = (samples / 'summary.expected.csv').read_bytes()
    # 第一关既核对交付样例，也重新执行，以识别只复制期望文件的交付。
    compare(regular(a, 'normalized.sample.csv').read_bytes(), normalized, 'A 样例')
    compare(regular(b, 'summary.sample.csv').read_bytes(), summary, 'B 样例')
    compare(run_csv(a, 'normalize.py', samples / 'raw.csv', checks / 'sample-normalized.csv'), normalized, 'A 复跑样例')
    compare(run_csv(b, 'summarize.py', samples / 'normalized.expected.csv', checks / 'sample-summary.csv'), summary, 'B 复跑样例')
    print('✅ 第一关：样例逐字节一致', flush=True)
    # 第二关强制加载指定测试模块，零测试不能算成功；完整输出留作证据。
    runner = ('import sys,unittest; sys.path.insert(0,sys.argv[1]); '
              'suite=unittest.defaultTestLoader.loadTestsFromName(sys.argv[2]); '
              'count=suite.countTestCases(); '
              'result=unittest.TextTestRunner(verbosity=2).run(suite); '
              'sys.exit(0 if count>=4 and result.wasSuccessful() else 1)')
    for directory, module in ((a, 'test_normalize'), (b, 'test_summarize')):
        regular(directory, module + '.py')
        rc, out, err = execute([sys.executable, '-I', '-c', runner, directory, module], directory)
        (checks / (module + '.log')).write_bytes(out + err)
        if rc:
            raise CheckError(module + ' 未通过，或测试不足 4 项')
    # 补充父级边界用例，不仅依赖工人自己写的测试。
    empty = checks / 'empty.csv'
    empty.write_bytes(b'name,category,amount\n')
    compare(run_csv(a, 'normalize.py', empty, checks / 'empty-a.csv'), empty.read_bytes(), 'A 空数据')
    compare(run_csv(b, 'summarize.py', empty, checks / 'empty-b.csv'), b'category,count,total\n', 'B 空数据')
    for directory, script, invalid in ((a, 'normalize.py', b'name,category,amount\nx,a,-1\n'),
                                        (b, 'summarize.py', b'name,category,amount\nx,A,01\n')):
        bad = checks / ('bad-' + script + '.csv')
        bad.write_bytes(invalid)
        output = checks / ('invalid-' + script + '.csv')
        rc, _, err = execute([sys.executable, directory / script, bad, output], directory)
        if rc == 0 or not err or output.exists():
            raise CheckError(script + ' 没有按契约拒绝坏输入')
    print('✅ 第二关：两边测试及父级边界检查通过', flush=True)
    # 第三关只把 A 的新输出交给 B，用另一份固定期望独立复验。
    mid = checks / 'e2e-normalized.csv'
    run_csv(a, 'normalize.py', samples / 'e2e.raw.csv', mid)
    compare(run_csv(b, 'summarize.py', mid, checks / 'e2e-summary.csv'),
            (samples / 'e2e.summary.expected.csv').read_bytes(), '端到端结果')
    print('✅ 第三关：端到端逐字节一致', flush=True)


def grade(directory, question):
    """校准评分覆盖正常与错误输入，输出按字节检查，不让工人提供分数。"""
    script = regular(directory, 'solve.py')
    for data, expected in question['cases']:
        rc, out, _ = execute([sys.executable, script], directory, data.encode('utf-8'))
        if rc or out != expected.encode('utf-8'):
            raise CheckError(question['id'] + ' 正常用例未通过')
    for data in question.get('invalid', []):
        rc, out, _ = execute([sys.executable, script], directory, data.encode('utf-8'))
        if not rc or out:
            raise CheckError(question['id'] + ' 非法输入用例未通过')

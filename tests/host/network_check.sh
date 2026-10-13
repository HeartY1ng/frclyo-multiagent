#!/bin/bash
# 在 Claude 所在 macOS 主机运行五项实测；不会读取或写入真实钥匙串。
# 用法：bash tests/host/network_check.sh [本机 models.json 路径] [OpenCode 可执行文件]
# 仅真实模型子项使用环境 DEEPSEEK_API_KEY；key 经环境注入假 security，不写任何文件。
set -u
BIN_DIR=$(cd "$(dirname "$0")/../../bin" && pwd -P) || exit 2
OC_BIN="${2:-$(command -v opencode)}"
export PYTHONDONTWRITEBYTECODE=1
exec python3 -I - "$BIN_DIR" "${1:-}" "$OC_BIN" <<'PY'
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time

# 结果统一汇总；异常也要打印明确的 ❌，不把“没能启动”误报成“成功拦住”。
bin_dir = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(bin_dir))
from frclyo import config as shared_config
# 只读配置定位模型源；假 HOME 里所有配置仍由统一公共库解析。
original_cfg = shared_config.load()
config_dir = bin_dir.parent / 'workers' / 'opencode'
real_bin = Path(sys.argv[3]).resolve()
models_source = Path(sys.argv[2] or original_cfg.get('models_snapshot_source') or
                     str(Path(os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache'))) / 'opencode' / 'models.json')).expanduser()
existing_snapshot = Path(original_cfg['runtime_dir']) / 'models.snapshot.json'
if not sys.argv[2] and existing_snapshot.is_file():
    models_source = existing_snapshot
labels = ['① urllib 出网被代理拒绝且留日志', '② 直接连接非白名单回环端口被系统拒绝',
          '③ 真实 DeepSeek 工人完成小任务', '④ 工人读不到假密钥哨兵',
          '⑤ 超时、TERM、KILL 后代理和工人退出']
results = {}
children = []
infos = []
root = Path(tempfile.mkdtemp(prefix='opencode-network-check-')).resolve()
os.chmod(root, 0o700)
print('证据目录：' + str(root), flush=True)

def report(index, ok, detail):
    results[index] = bool(ok)
    print(('✅ ' if ok else '❌ ') + labels[index] + '：' + detail, flush=True)

def load(path):
    return json.loads(path.read_text(encoding='utf-8'))

def await_file(path, proc, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            try:
                return load(path)
            except (ValueError, OSError):
                pass
        if proc.poll() is not None:
            raise RuntimeError('工人提前退出，见 ' + str(path.parent / 'launcher.log'))
        time.sleep(0.05)
    raise RuntimeError('等待就绪超时：' + str(path))

def alive(pid):
    # ps 可把已退出但尚待系统回收的僵尸与真正运行中的进程区别开。
    status = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
    return status.returncode == 0 and bool(status.stdout.strip()) and not status.stdout.strip().startswith('Z')

def gone(info, worker_pid=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not alive(info['代理进程号']) and (worker_pid is None or not alive(worker_pid)):
            try:
                with socket.create_connection(('127.0.0.1', info['端口']), timeout=0.3):
                    pass
            except OSError as exc:
                return exc.errno == errno.ECONNREFUSED
        time.sleep(0.1)
    return False

# 所有探针从被交付的 opencode-worker.sh 真入口启动，只把模型 CLI 换成确定性的哨兵程序。
# 不另外抄一套沙箱规则；真实模型调用也使用同一入口。生产脚本不会读取这些探针模式。
probe_source = r'''
import errno, json, os, pathlib, socket, subprocess, sys, time, urllib.request
run = pathlib.Path(sys.argv[sys.argv.index('--dir') + 1])
mode = sys.stdin.read().strip()
if mode == 'hold':
    # 子进程用于验证超时不是只杀 OpenCode 主进程。
    child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(300)'])
    (run / 'probe-ready.json').write_text(json.dumps({'pid': os.getpid(), 'child': child.pid}))
    time.sleep(300)
else:
    (run / 'probe-ready.json').write_text(json.dumps({'pid': os.getpid()}))
    result = {}
    # urllib 自动读取 worker 设置的 HTTPS_PROXY；明确要求 403，DNS 失败或端口不通不算通过。
    try:
        urllib.request.urlopen('https://example.com/', timeout=8).read(1)
        result['urllib'] = False
    except Exception as exc:
        result['urllib'] = '403 Forbidden' in str(exc)
        result['urllib_error'] = str(exc)
    # 要求 EPERM/EACCES，不能把目标端口恰好没有监听的 ECONNREFUSED 当作隔离证据。
    try:
        with socket.create_connection(('127.0.0.1', int(os.environ['FRCLYO_TEST_BLOCKED_PORT'])), timeout=3):
            result['blocked_port'] = False
    except OSError as exc:
        result['blocked_port'] = exc.errno in (errno.EPERM, errno.EACCES)
        result['blocked_port_errno'] = exc.errno
    # 再检查绕过代理的公网 IP 拨号，不依赖沙箱内 DNS 是否可用。
    try:
        with socket.create_connection(('1.1.1.1', 443), timeout=3):
            result['direct'] = False
    except OSError as exc:
        result['direct'] = exc.errno in (errno.EPERM, errno.EACCES)
    # tcp4 不能顺便放行 IPv6 同端口；不依赖 IPv6 端口有无服务，也必须是权限拒绝。
    from urllib.parse import urlsplit
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as conn:
            conn.settimeout(3)
            conn.connect(('::1', urlsplit(os.environ['HTTPS_PROXY']).port))
            result['ipv6'] = False
    except OSError as exc:
        result['ipv6'] = exc.errno in (errno.EPERM, errno.EACCES)
    result['secrets'] = True
    for path in (pathlib.Path.home() / '.frclyo' / 'sentinel', pathlib.Path.home() / '.ssh' / 'sentinel'):
        try:
            path.read_bytes()
            result['secrets'] = False
        except OSError as exc:
            result['secrets'] = result['secrets'] and exc.errno in (errno.EPERM, errno.EACCES)
    # 验证拒绝证据不可篡改；测试失败时只追加固定字符串，不含真实 key。
    try:
        with open(run / '代理拒绝.log', 'a') as stream:
            stream.write('tamper-sentinel\n')
        result['log_protected'] = False
    except OSError as exc:
        result['log_protected'] = exc.errno in (errno.EPERM, errno.EACCES)
    (run / 'probe-result.json').write_text(json.dumps(result))
    print(json.dumps({'type': 'text', 'part': {'text': 'probe-complete'}}), flush=True)
'''

# 运行 shell 包装器时对路径正确加引号；路径即使有空格也不改变命令含义。
import shlex
probe_py = root / 'probe.py'
probe_py.write_text(textwrap.dedent(probe_source), encoding='utf-8')
probe = root / 'probe-opencode'
probe.write_text('#!/bin/bash\n# 测试专用确定性工人，不调用模型。\nexec ' + shlex.quote(sys.executable) +
                 ' -I ' + shlex.quote(str(probe_py)) + ' "$@"\n', encoding='utf-8')
probe.chmod(0o700)
base_env = os.environ.copy()
real_key = base_env.pop('DEEPSEEK_API_KEY', '')
# 不让探针继承派活方传来的输入目录或其他生产路径覆盖。
for key in ('WORKER_INPUT_DIR', 'FRCLYO_CONFIG', 'OPENCODE_CONFIG', 'OPENCODE_CONFIG_CONTENT', 'XDG_CACHE_HOME'):
    base_env.pop(key, None)


def launch(name, mode, timeout, actual=False):
    area = root / name
    home = area / 'home'
    # 与生产相同：配置和交付都在 HOME 下，密钥禁读/家目录禁写有相同的祖先关系。
    config = home / '.frclyo'
    deliver = home / 'work' / '交付'
    commands = area / 'commands'
    for path in (home / '.ssh', config, deliver, commands):
        path.mkdir(parents=True, exist_ok=True)
    # shim 仅在测试 PATH 中，把环境中的值返回 keys.get_key；不接触主机钥匙串。
    security = commands / 'security'
    security.write_text('#!/bin/bash\n# 测试专用钥匙串替身。\n[ "$1" = find-generic-password ] || exit 2\nprintf %s "$FRCLYO_TEST_KEY"\n')
    security.chmod(0o700)
    (commands / 'opencode').symlink_to(real_bin if actual else probe)
    for dirname in ('.frclyo', '.ssh'):
        sentinel = home / dirname / 'sentinel'
        sentinel.write_text('FAKE-SECRET-SENTINEL-' + name)
        assert sentinel.read_text().startswith('FAKE-SECRET-SENTINEL-')
    # 配置没有 key，只指定工作目录、权限模板和本机模型目录源。
    config_file = config / 'config.json'
    config_file.write_text(json.dumps({'work_dir': str(home / 'work'),
                                       'opencode_config': str(config_dir / 'opencode.worker.jsonc'),
                                       'models_snapshot_source': str(models_source)}))
    task = area / 'task.txt'
    task.write_text(mode, encoding='utf-8')
    env = base_env.copy()
    env.update(HOME=str(home), FRCLYO_CONFIG=str(config_file),
               PATH=str(commands) + os.pathsep + base_env.get('PATH', ''),
               FRCLYO_TEST_KEY=real_key if actual else 'fake-offline-key')
    run = deliver / 'NETWORK-CHECK' / name
    with open(area / 'launcher.log', 'wb') as output:
        proc = subprocess.Popen(['/bin/bash', str(bin_dir / 'opencode-worker.sh'), 'NETWORK-CHECK', name,
                                 str(task), 'deepseek/deepseek-flash', 'nothink', str(timeout)],
                                env=env, stdout=output, stderr=subprocess.STDOUT)
    children.append(proc)
    return proc, run


def netinfo(run, proc):
    info = await_file(run / '网络信息.json', proc)
    infos.append(info)
    return info

# 外部中断也回收实测自己启动的进程；绝不按进程名称全局 pkill。
def interrupted(sig, frame):
    raise KeyboardInterrupt('实测收到信号 ' + str(sig))
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, interrupted)

listener = None
try:
    # 前置检查不碰网络 API，也不读真实密钥文件。真正的 TCP 单测在本机开启，不允许跳过。
    if sys.platform != 'darwin' or not shutil.which('sandbox-exec'):
        raise RuntimeError('需要 macOS sandbox-exec')
    if not (config_dir / 'opencode.worker.jsonc').is_file():
        raise RuntimeError('缺少配置文件：' + str(config_dir))
    if not models_source.is_file():
        raise RuntimeError('缺少本机模型目录；先运行 opencode models --refresh，或用第一个参数传入 models.json')
    test_env = base_env.copy()
    unit = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(bin_dir.parent / 'tests/host'), '-p', 'test_proxy_tcp.py', '-v'],
                          cwd=bin_dir, env=test_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
    (root / '代理单测.log').write_text(unit.stdout, encoding='utf-8')
    if unit.returncode:
        raise RuntimeError('代理/监督器单测失败，见代理单测.log')
    print('代理 TCP 生命周期单测：通过', flush=True)

    # 用临时监听证明端口在沙箱外可连，内核拒绝才算隔离成功。
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(8)
    blocked_port = listener.getsockname()[1]
    base_env['FRCLYO_TEST_BLOCKED_PORT'] = str(blocked_port)
    with socket.create_connection(('127.0.0.1', blocked_port), timeout=1):
        pass
    proc, run = launch('sentinels', 'probe', 30)
    info = netinfo(run, proc)
    rc = proc.wait(timeout=45)
    data = load(run / 'probe-result.json')
    receipt = load(run / '回执.json')
    logs = [json.loads(x) for x in (run / '代理拒绝.log').read_text().splitlines()]
    network = receipt['网络']
    report(0, rc == 0 and data['urllib'] and data['direct'] and data['ipv6'] and data['log_protected'] and
           any(x['目标'] == 'example.com:443' for x in logs) and
           network['放行'] == ['api.deepseek.com:443'] and network['拒绝次数'] == len(logs) and len(logs) >= 1,
           '校验 403、公网直连被拒、日志保护和回执计数；证据在 sentinels/')
    report(1, rc == 0 and data['blocked_port'], 'errno=' + str(data.get('blocked_port_errno')) + '，外部已证明端口可连')
    report(3, rc == 0 and data['secrets'], '只读取临时假 HOME 下的敏感目录哨兵')
    lifecycle_ok = gone(info)

    # 生命周期用假工人，不消耗模型费用；按 PID 和端口分别确认真正退出。
    for name, sig, timeout in (('timeout', None, 3), ('term', signal.SIGTERM, 60), ('kill', signal.SIGKILL, 60)):
        try:
            proc, run = launch(name, 'hold', timeout)
            info = netinfo(run, proc)
            ready = await_file(run / 'probe-ready.json', proc)
            # 等待子进程 PID 落盘，确保测到了整组清理。
            deadline = time.monotonic() + 2
            while 'child' not in ready and time.monotonic() < deadline:
                time.sleep(0.02)
                ready = load(run / 'probe-ready.json')
            if sig is not None:
                proc.send_signal(sig)
            rc = proc.wait(timeout=15)
            expected = 142 if sig is None else (143 if sig == signal.SIGTERM else -signal.SIGKILL)
            ok = rc == expected and gone(info, ready['pid']) and 'child' in ready and not alive(ready['child'])
            if sig != signal.SIGKILL:
                receipt = load(run / '回执.json')
                # 超时除了 142 还必须落“超时”回执；TERM 沿用原有“失败”状态和网络字段。
                expected_status = '超时' if sig is None else '失败'
                ok = ok and receipt['退出码'] == expected and receipt['状态'] == expected_status and \
                     receipt['网络'] == {'放行': ['api.deepseek.com:443'], '拒绝次数': 0}
            lifecycle_ok = lifecycle_ok and ok
            print(('✅ ' if ok else '❌ ') + '回收子项 ' + name + '：退出码=' + str(rc), flush=True)
        except Exception as exc:
            lifecycle_ok = False
            print('❌ 回收子项 ' + name + '：' + str(exc), flush=True)
    report(4, lifecycle_ok, '正常结束、超时、TERM、KILL 均检查 PID 和监听端口；KILL 不要求回执')

    # 最后只启动一次真实 DeepSeek 任务：模型必须创建含固定哨兵的文件，不能只看进程退出码。
    if not real_key or '\n' in real_key or '\r' in real_key:
        report(2, False, '请由 Claude 在本机环境注入 DEEPSEEK_API_KEY 后重跑；没有读取真实密钥文件')
    elif not real_bin.is_file() or not os.access(real_bin, os.X_OK):
        report(2, False, '找不到 OpenCode：' + str(real_bin))
    else:
        prompt = '这是网络隔离验收。只做一件事：在本轮工作目录创建 model-sentinel.txt，内容严格为 MODEL_API_OK 换行。然后回复 MODEL_API_OK。不要读其他文件，不要联网，不要运行其他任务。'
        proc, run = launch('model', prompt, 180, actual=True)
        info = netinfo(run, proc)
        rc = proc.wait(timeout=200)
        receipt = load(run / '回执.json')
        artifact = run / 'model-sentinel.txt'
        ok = rc == 0 and receipt['状态'] == '执行完成' and receipt['工具调用数'] >= 1 and artifact.is_file() and \
             artifact.read_text().strip() == 'MODEL_API_OK' and receipt['网络']['放行'] == ['api.deepseek.com:443'] and gone(info)
        report(2, ok, '真实模型和 Bun 代理兼容性；详见 model/ 下事件、stderr、回执和文件哨兵')
except BaseException as exc:
    print('实测异常：' + str(exc), flush=True)
finally:
    # 所有退出路径都尽量清理；保留日志和哨兵，key 只存在内存和受控子进程环境。
    for proc in children:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
    for info in infos:
        if alive(info['代理进程号']):
            try:
                os.kill(info['代理进程号'], signal.SIGTERM)
            except ProcessLookupError:
                pass
    if listener is not None:
        listener.close()
    real_key = ''
    for index in range(5):
        if index not in results:
            report(index, False, '前置失败或实测中断，未验证')
    passed = sum(results.values())
    print('汇总：%s/5 通过；证据：%s' % (passed, root), flush=True)
    (root / '汇总.json').write_text(json.dumps({labels[k]: v for k, v in results.items()}, ensure_ascii=False, indent=2))
sys.exit(0 if all(results.values()) else 1)
PY

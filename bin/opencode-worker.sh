#!/bin/bash
# opencode-worker.sh —— OpenCode 国产模型工人的唯一入口；兼容 macOS bash 3.2。
# 每轮只注入一家 key，并用系统沙箱把该进程及其子命令的网络锁到本轮白名单代理。
# key 仍在环境变量里；即使子命令取到它，普通 TCP/UDP/Unix socket 也不能另找出口，
# 只能经代理发到这家的官方 API:443。CONNECT 不解密 TLS，也不约束官方 API 内的路径。
# 用法：脚本 <任务编号> <轮次编号> <任务说明文件> <家/模型> [变体] [超时秒，默认1800]
# 退出码：0 成功；2 参数/准备失败；3 轮次已存在；4 建目录失败；142 超时；128+信号 中断。
set -u
umask 077

# ---------- 1. 检查参数，阻止路径穿越；0 秒不算正整数，避免意外取消超时 ----------
TASK_ID="${1:-}"; RUN_ID="${2:-}"; TASK_FILE="${3:-}"; MODEL="${4:-}"; VARIANT="${5:-}"; TIMEOUT_SEC="${6:-1800}"
if [ -z "$TASK_ID" ] || [ -z "$RUN_ID" ] || [ -z "$TASK_FILE" ] || [ -z "$MODEL" ]; then
  echo "用法：opencode-worker.sh <任务编号> <轮次编号> <任务说明文件> <家/模型> [变体] [超时秒数]" >&2; exit 2
fi
for v in "$TASK_ID" "$RUN_ID"; do
  [[ "$v" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "编号只能含字母、数字、-、_：$v" >&2; exit 2; }
done
[[ "$TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] || { echo "超时秒数必须是正整数：$TIMEOUT_SEC" >&2; exit 2; }
[[ "$MODEL" =~ ^[a-z0-9-]+/[A-Za-z0-9._-]+$ ]] || { echo "模型格式应为 家/模型：$MODEL" >&2; exit 2; }
[[ -z "$VARIANT" || "$VARIANT" =~ ^[a-z]+$ ]] || { echo "变体格式不对：$VARIANT" >&2; exit 2; }
[ -f "$TASK_FILE" ] || { echo "找不到任务说明文件：$TASK_FILE" >&2; exit 2; }

# ---------- 2. 提供方集合、路径、主机和模型模板统一通过 config 读取 ----------
BIN_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 2
SETTINGS=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from frclyo.worker import main; sys.exit(main())' "$BIN_DIR" opencode "$MODEL") || exit 2
field() { python3 -I -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$SETTINGS" "$1"; }
DELIVER=$(field deliver) || exit 2
OC=$(field runtime) || exit 2
OC_CONFIG=$(field template) || exit 2
MODELS_PATH=$(field snapshot) || exit 2
API_TARGET=$(field target) || exit 2
ACTUAL_MODEL=$(field model) || exit 2
CONFIG_CONTENT=$(field content) || exit 2
SENSITIVE=$(field sensitive) || exit 2
PROTECTED=$(field protected) || exit 2
OC_BIN=$(command -v opencode) || { echo "找不到 opencode，请先安装并加入 PATH" >&2; exit 2; }
SANDBOX_BIN=$(command -v sandbox-exec) || { echo "需要 macOS sandbox-exec" >&2; exit 2; }
[ -x "$OC_BIN" ] && [ -f "$OC_CONFIG" ] && [ -f "$BIN_DIR/allowlist-proxy.py" ] || {
  echo "缺少 OpenCode 可执行文件、权限模板或同目录代理" >&2; exit 2;
}
# ---------- 3. 原子占用本轮目录，禁止并发覆盖 ----------
RUN_DIR="$DELIVER/$TASK_ID/$RUN_ID"
if [ -e "$RUN_DIR" ]; then echo "已存在 ${RUN_DIR}，拒绝重复执行" >&2; exit 3; fi
# mkdir 不用 -p 建最后一级，两个并发派活也不能同时占用同一轮。
mkdir -p "$DELIVER/$TASK_ID" || exit 4
mkdir "$RUN_DIR" || exit 4
RUN_DIR=$(cd "$RUN_DIR" && pwd -P) || exit 4
cp "$TASK_FILE" "$RUN_DIR/任务.txt" || exit 4
if [ -n "${WORKER_INPUT_DIR:-}" ]; then
  [ -d "$WORKER_INPUT_DIR" ] || { echo "找不到输入目录：$WORKER_INPUT_DIR" >&2; exit 2; }
  mkdir "$RUN_DIR/输入" && cp -R "$WORKER_INPUT_DIR"/. "$RUN_DIR/输入/" || exit 4
fi

# ---------- 4. OpenCode 状态仍定向 runtime；代理控制目录放在其外，避免可写祖先被整体改名 ----------
mkdir -p "$OC/runtime"/{config,data,state,cache,tmp} || exit 4
OC=$(cd "$OC" && pwd -P) || exit 4
RT="$OC/runtime"
CONTROL=$(mktemp -d "$OC/.network-$TASK_ID-$RUN_ID.XXXXXX") || exit 4
PROFILE="$CONTROL/sandbox.sb"
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
PROXY_PID=""; WORKER_PID=""; PROXY_PORT=""; RUN_STARTED=0

# ---------- 5. 回执仍从事件流提取事实；拒绝计数读代理退出时的内存计数，不信任工人文件 ----------
write_receipt() {
  python3 -I - "$RUN_DIR" "$TASK_ID" "$RUN_ID" "$1" "$START" "$2" "$MODEL" "$VARIANT" "$API_TARGET" "$CONTROL/代理.stdout" <<'PY'
import json, sys, hashlib, os
# 只用固定位置的参数，不执行工人输出的任何内容。
d, task, run, rc, st, en, model, variant, target, stats = sys.argv[1:]
texts, tools, errors, tokens, seen_models = [], 0, [], {}, set()
try:
    for line in open(os.path.join(d, 'events.jsonl'), encoding='utf-8'):
        try:
            e = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(e, dict):
            continue
        t, part = e.get('type'), e.get('part') or {}
        if not isinstance(part, dict):
            continue
        if t == 'text' and isinstance(part.get('text'), str) and part['text']:
            texts.append(part['text'])
        elif t == 'tool_use':
            tools += 1
        elif t == 'error':
            errors.append(json.dumps(e, ensure_ascii=False)[:300])
        elif t == 'step_finish':
            for k, v in (part.get('tokens') or {}).items():
                if isinstance(v, (int, float)):
                    tokens[k] = tokens.get(k, 0) + v
        for key in ('modelID', 'model'):
            m = part.get(key) or e.get(key)
            if isinstance(m, str):
                seen_models.add(m)
except FileNotFoundError:
    pass
# 控制目录对工人不可读写；代理意外崩溃时不伪造 0 次，而将计数写 null 并报错。
try:
    with open(stats, encoding='utf-8') as stream:
        lines = stream.read().splitlines()
    denied = int(json.loads(lines[-1])['拒绝次数'])
except (OSError, ValueError, KeyError, TypeError, IndexError):
    denied = None
    errors.append('代理未留下可信退出计数，请检查代理.stderr 和代理拒绝.log')
res = os.path.join(d, '结果.txt')
if texts:
    open(res, 'w', encoding='utf-8').write(texts[-1].strip() + '\n')
sha = hashlib.sha256(open(res, 'rb').read()).hexdigest() if os.path.exists(res) else None
ok = (rc == '0' and sha is not None and not errors)
json.dump({'工人': 'opencode', '模型': model, '变体': variant or '默认',
           '事件中出现的模型': sorted(seen_models),
           '任务编号': task, '轮次编号': run, '退出码': int(rc),
           '开始': st, '结束': en, '工具调用数': tools, '用量': tokens or None,
           '错误': errors, '结果sha256': sha,
           '网络': {'放行': [target], '拒绝次数': denied},
           '状态': '执行完成' if ok else ('超时' if rc == '142' else '失败')},
          open(os.path.join(d, '回执.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
PY
}

# ---------- 6. 所有可捕获退出都先收工人、再收代理；强杀则靠两者各自的父进程检查 ----------
cleanup() {
  local saved_rc=$?
  trap - EXIT
  trap '' HUP INT TERM
  if [ -n "$WORKER_PID" ]; then
    stop_child "$WORKER_PID"
  fi
  if [ -n "$PROXY_PID" ]; then
    stop_child "$PROXY_PID"
  fi
  if [ "$RUN_STARTED" = 1 ]; then
    write_receipt "$saved_rc" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" || saved_rc=2
  fi
  # 控制目录留作诊断，位于配置目录的 .network-*；不改原 runtime 的结构。
  exit "$saved_rc"
}
# 先恢复可能被暂停的子进程，让 TERM 有机会被处理；最多等五秒，再强杀，防止清理永久卡住。
stop_child() {
  local child_pid="$1" attempt
  kill -CONT "$child_pid" 2>/dev/null || :
  kill -TERM "$child_pid" 2>/dev/null || :
  for ((attempt=0; attempt<50; attempt++)); do
    kill -0 "$child_pid" 2>/dev/null || break
    sleep 0.1
  done
  kill -KILL "$child_pid" 2>/dev/null || :
  wait "$child_pid" 2>/dev/null || :
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# ---------- 7. key 由 Python 监督器经 keys.get_key 直接注入内存环境 ----------
# 不在 shell 中打印、捕获或落盘 key；keys 读取失败就停止，不启动模型。

# ---------- 8. 先在沙箱外启动本轮代理；第一行端口必须有效，否则绝不启动工人 ----------
python3 -I "$BIN_DIR/allowlist-proxy.py" --allow "$API_TARGET" \
  --log "$RUN_DIR/代理拒绝.log" --parent-pid "$$" \
  > "$CONTROL/代理.stdout" 2> "$CONTROL/代理.stderr" &
PROXY_PID=$!
for ((i=0; i<100; i++)); do
  if [ -s "$CONTROL/代理.stdout" ]; then IFS= read -r PROXY_PORT < "$CONTROL/代理.stdout" || :; fi
  [ -n "$PROXY_PORT" ] && break
  kill -0 "$PROXY_PID" 2>/dev/null || break
  sleep 0.1
done
[[ "$PROXY_PORT" =~ ^[0-9]+$ ]] && [ "$PROXY_PORT" -gt 0 ] && [ "$PROXY_PORT" -le 65535 ] || {
  echo "代理未能就绪，查看 $CONTROL/代理.stderr" >&2; exit 2;
}

# ---------- 9. 网络默认全禁，只允许精确的 IPv4 回环 TCP 端口；不开放 DNS、其他回环端口或 Unix socket ----------
# macOS 的 remote 地址语法只接受 localhost/*，不接受字面量 127.0.0.1；tcp4 限定 IPv4，
# 因此 tcp4 + localhost + 精确端口对应 127.0.0.1:本轮端口，不放行 IPv6 或整段 localhost。
# 用 Python 转义路径，避免空格、引号或反斜线破坏规则；realpath 同时覆盖 macOS 的 /tmp 别名。
# 拒绝日志只让沙箱外代理写，私有控制目录禁止读写，防止工人篡改规则或计数。
python3 -I - "$HOME" "$RUN_DIR" "$RT" "$CONTROL" "$PROXY_PORT" "$PROFILE" "$PROXY_PID" "$API_TARGET" "$SENSITIVE" "$PROTECTED" <<'PY'
import json, os, sys
home, run, rt, control, port, profile, pid, target, sensitive, protected = sys.argv[1:]
# Seatbelt 字符串支持引号和反斜线转义；路径中的控制字符直接拒绝，保持规则单义。
def quote(path):
    path = os.path.realpath(path)
    if any(ord(c) < 32 for c in path):
        raise ValueError('路径不能含控制字符')
    return json.dumps(path, ensure_ascii=False)
secret = ['.hermes', '.frclyo', '.codex', '.claude', '.ssh', '.gnupg', '.config', '.aws',
          '.docker', '.netrc', '.local/share/opencode', 'Library/Keychains']
rules = ['(version 1)', '(allow default)',
         '(deny file-read* ' + ' '.join('(subpath %s)' % quote(p) for p in ([os.path.join(home, s) for s in secret] + json.loads(sensitive))) + ')',
         '(deny file-write* (subpath %s))' % quote(home),
         # 工作目录搬到 HOME 外仍保护运行时父目录与模板，防止整体改名绕过控制目录。
         '(deny file-write* ' + ' '.join('(subpath %s)' % quote(p) for p in json.loads(protected)) + ')',
         '(allow file-write* (subpath %s) (subpath %s))' % (quote(run), quote(rt)),
         '(deny file-read* file-write* (subpath %s))' % quote(control),
         '(deny file-write* ' + ' '.join('(subpath %s)' % quote(p) for p in json.loads(sensitive)) + ')',
         '(deny file-write* (literal %s) (literal %s))' %
         (quote(os.path.join(run, '代理拒绝.log')), quote(os.path.join(run, '网络信息.json'))),
         '(deny network*)',
         '(allow network-outbound (remote tcp4 "localhost:%s"))' % port]
open(profile, 'w', encoding='utf-8').write('\n'.join(rules) + '\n')
# 仅给本机实测和排障使用，不含 key；端口不是凭据，不能扩大这份沙箱的网络权限。
json.dump({'代理进程号': int(pid), '端口': int(port), '放行': [target], '规则': profile},
          open(os.path.join(run, '网络信息.json'), 'w', encoding='utf-8'), ensure_ascii=False)
PY
[ "$?" = 0 ] || exit 2
printf '{"工人":"opencode","模型":"%s","档位":"%s","开始":"%s","超时秒":%s,"进程号":%s}\n' \
  "$MODEL" "${VARIANT:-默认}" "$START" "$TIMEOUT_SEC" "$$" > "$RUN_DIR/运行信息.json"
RUN_STARTED=1

# ---------- 10. 清掉别家的 key 和所有旧代理；大小写变量一致，NO_PROXY 不能绕过白名单 ----------
(
  unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL OPENAI_API_KEY GITHUB_TOKEN GH_TOKEN \
        MOONSHOT_API_KEY KIMI_API_KEY OPENCODE_API_KEY OPENROUTER_API_KEY GEMINI_API_KEY \
        DEEPSEEK_API_KEY KIMI_CN_API_KEY XIAOMI_API_KEY
  unset ALL_PROXY all_proxy NO_PROXY no_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy
  export HTTPS_PROXY="http://127.0.0.1:$PROXY_PORT" HTTP_PROXY="http://127.0.0.1:$PROXY_PORT"
  export https_proxy="$HTTPS_PROXY" http_proxy="$HTTP_PROXY" NO_PROXY='' no_proxy=''
  export XDG_CONFIG_HOME="$RT/config" XDG_DATA_HOME="$RT/data" XDG_STATE_HOME="$RT/state" XDG_CACHE_HOME="$RT/cache" TMPDIR="$RT/tmp/"
  export OPENCODE_CONFIG="$OC_CONFIG" OPENCODE_CONFIG_CONTENT="$CONFIG_CONTENT"
  export OPENCODE_DISABLE_PROJECT_CONFIG=1 OPENCODE_DISABLE_CLAUDE_CODE=1 OPENCODE_DISABLE_EXTERNAL_SKILLS=1
  export OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_SHARE=1 OPENCODE_DISABLE_LSP_DOWNLOAD=1
  export OPENCODE_DISABLE_DEFAULT_PLUGINS=1 OPENCODE_AUTH_CONTENT='{}'
  export OPENCODE_DISABLE_MODELS_FETCH=1 OPENCODE_MODELS_PATH="$MODELS_PATH"
  # 参数数组始终非空；不展开 bash 3.2 在 set -u 下会报错的空数组。
  ARGS=("$OC_BIN" run --pure --log-level WARN --dir "$RUN_DIR" -m "$ACTUAL_MODEL")
  if [ -n "$VARIANT" ]; then ARGS+=(--variant "$VARIANT"); fi
  ARGS+=(--agent worker --title "worker-$TASK_ID-$RUN_ID" --format json)
  # 监督器在沙箱外，工人在独立进程组内。超时/收到信号/父 shell 被强杀时均收整个进程组。
  # Python 也不用系统代理；此处只管理进程，不联网。stdin 显式打开任务文件，后台运行不会丢输入。
  exec python3 -I -c '
import os, signal, subprocess, sys, time
# 加载可信公共库后在内存注入本家 key；参数移走以保持监督器原有 argv 协议。
bin_dir, provider = sys.argv[1:3]
del sys.argv[1:3]
sys.path.insert(0, bin_dir)
from frclyo.worker import inject_key
try:
    inject_key(provider)
except (OSError, ValueError):
    sys.exit("未找到提供方 key 或配置无效；请先运行 aw key add")
seconds, parent, proxy, task = int(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
reason = [0]
# 信号回调只记下原因，由主循环统一回收，避免在回调里重入 subprocess。
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, lambda sig, frame: reason[0] or reason.__setitem__(0, 128 + sig))
if os.getppid() != parent:
    sys.exit(143)
with open(task, "rb") as source:
    child = subprocess.Popen(sys.argv[5:], stdin=source, start_new_session=True)
deadline = time.monotonic() + seconds
# macOS 对只有僵尸的组也可能报 EPERM；它不是 ESRCH，也可能是真正的权限拒绝。
# 每次 EPERM 先 poll 回收直接子进程，再重试；仍被拒绝时返回未知，绝不冒充组已消失。
def signal_group(sig):
    for attempt in range(2):
        try:
            os.killpg(child.pid, sig)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            child.poll()
    return None

def group_running():
    child.poll()
    exists = signal_group(0)
    if exists is not None:
        return exists
    # 孙进程的僵尸只能由其父进程/系统回收；直接子进程退出不代表整组已经退出。
    # 仅在持续 EPERM 时用系统 ps 区分僵尸与活进程；查不到可靠状态就继续按存活处理。
    try:
        snapshot = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="],
                                  capture_output=True, text=True, timeout=0.2, check=True)
        for line in snapshot.stdout.splitlines():
            pgid, state = line.split()
            if int(pgid) == child.pid and not state.startswith("Z"):
                return True
        return child.poll() is None
    except (OSError, ValueError, subprocess.SubprocessError):
        return True

# 代理意外消失也立即停工；不尝试使用其他联网方式。
while child.poll() is None and not reason[0]:
    if os.getppid() != parent:
        reason[0] = 143
    elif time.monotonic() >= deadline:
        reason[0] = 142
    else:
        try:
            os.kill(proxy, 0)
        except ProcessLookupError:
            reason[0] = 125
        except PermissionError:
            # 代理不是监督器的子进程，不能 wait 它；权限拒绝时保守停工，由 shell 收代理。
            child.poll()
            reason[0] = 125
    time.sleep(0.1)
# 即使主进程正常退出，也结束同组残留子命令；两秒后强杀，禁止留下后台任务。
signal_group(signal.SIGTERM)
until = time.monotonic() + 2
while group_running() and time.monotonic() < until:
    time.sleep(0.05)
if group_running():
    signal_group(signal.SIGKILL)
    until = time.monotonic() + 1
    while group_running() and time.monotonic() < until:
        time.sleep(0.05)
# 真正无权终止或无法核实状态时明确留错，不用无期限 wait 卡住 shell 的回执和代理清理。
# 超时原因一经确定就保持 142，后来的信号或清理诊断不能覆盖它。
cleanup_failed = group_running()
try:
    rc = child.wait(timeout=0.1)
except subprocess.TimeoutExpired:
    cleanup_failed = True
    rc = 125
if cleanup_failed:
    print("监督器无法确认进程组已退出：pgid=%s，请检查进程权限/状态" % child.pid, file=sys.stderr)
sys.exit(reason[0] or (125 if cleanup_failed else (128 - rc if rc < 0 else rc)))
' "$BIN_DIR" "${MODEL%%/*}" "$TIMEOUT_SEC" "$$" "$PROXY_PID" "$RUN_DIR/任务.txt" \
    "$SANDBOX_BIN" -f "$PROFILE" "${ARGS[@]}"
) > "$RUN_DIR/events.jsonl" 2> "$RUN_DIR/stderr.log" &
WORKER_PID=$!
# Bash 等后台进程时能立即执行 trap；不要改回前台等待而延迟响应终止信号。
wait "$WORKER_PID"
RC=$?
WORKER_PID=""
exit "$RC"

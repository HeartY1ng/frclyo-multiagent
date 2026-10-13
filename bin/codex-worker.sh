#!/bin/bash
# Codex 工人入口：每轮独立目录，隔离用户配置并保留事件、结果和回执。
# 用法：脚本 <任务编号> <轮次编号> <任务说明文件> <模型> <思考档位> [超时秒，默认1800]
# 模型与档位必须显式指定；WEB/FAST/SUBAGENTS/WORKER_INPUT_DIR 开关沿用原接口。
set -u

# ---------- 1. 读参数并检查 ----------
TASK_ID="${1:-}"; RUN_ID="${2:-}"; TASK_FILE="${3:-}"; MODEL="${4:-}"; EFFORT="${5:-}"; TIMEOUT_SEC="${6:-1800}"
if [ -z "$TASK_ID" ] || [ -z "$RUN_ID" ] || [ -z "$TASK_FILE" ] || [ -z "$MODEL" ] || [ -z "$EFFORT" ]; then
  echo "用法：codex-worker.sh <任务编号> <轮次编号> <任务说明文件> <模型> <思考档位> [超时秒数]" >&2; exit 2
fi
# 编号只允许字母、数字、横线、下划线，防止路径穿越（比如 ../../）
for v in "$TASK_ID" "$RUN_ID"; do
  [[ "$v" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "编号只能含字母、数字、-、_：$v" >&2; exit 2; }
done
# 超时秒数必须是正整数（看板和计时器都要用它）
[[ "$TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] || { echo "超时秒数必须是正整数：$TIMEOUT_SEC" >&2; exit 2; }
# 模型和档位对照 Codex 自己的模型清单检查：只认公开列出的模型（隐藏的不用）、只认该模型支持的档位；清单读不到就拒绝，不猜
# 注意：这份清单命令行 codex 和 ChatGPT App 自带的 Codex 都会写，谁最后联网谁覆盖，两边看到的模型不一样（2026-10-13 实测）。
#       清单不是本机命令行版本写的，只提醒、不拦：真用了命令行不支持的模型，codex 自己会报错退出。
BIN_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 2
CLI_VER=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.models import cli_version; print(cli_version("codex"))' "$BIN_DIR") || exit 2
CODEX_CACHE=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from frclyo.config import codex_home; print(codex_home() / "models_cache.json")' "$BIN_DIR") || exit 2
python3 -I - "$MODEL" "$EFFORT" "${CODEX_WORKER_SUBAGENTS:-0}" "$CLI_VER" "$CODEX_CACHE" <<'EOF' || exit 2
import json, os, sys
model, effort, sub, cli_ver = sys.argv[1:5]
path = sys.argv[5]
try:
    cache = json.load(open(path, encoding='utf-8'))
    models = cache['models']
except Exception as e:
    sys.exit(f'读不到 Codex 模型清单 {path}：{e}')
if cache.get('client_version') != cli_ver:
    print(f'⚠️ 模型清单是 Codex {cache.get("client_version")} 写的，本机命令行是 {cli_ver}，可用模型可能对不上', file=sys.stderr)
listed = {m.get('slug'): m for m in models if m.get('visibility') == 'list'}
if model not in listed:
    sys.exit(f'不认识的模型：{model}。可用：{"、".join(listed)}')
levels = [l.get('effort') for l in listed[model].get('supported_reasoning_levels') or []]
if effort not in levels:
    sys.exit(f'{model} 不支持思考档位 {effort}。可用：{"/".join(levels)}')
if effort == 'ultra' and sub != '1':
    sys.exit('ultra 档会自动把任务分给子 agent，要同时设 CODEX_WORKER_SUBAGENTS=1 才放行')
EOF
[ -f "$TASK_FILE" ] || { echo "找不到任务说明文件：$TASK_FILE" >&2; exit 2; }

# ---------- 2. 算出本轮专属目录；已存在就拒绝（防重复执行、防覆盖旧结果）----------
# Python 隔离模式只信任脚本相邻的公共库；配置值不作为 shell 代码执行。
SETTINGS=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from frclyo.worker import main; sys.exit(main())' "$BIN_DIR" codex) || exit 2
field() { python3 -I -c 'import json,sys; v=json.loads(sys.argv[1])[sys.argv[2]]; print(int(v) if isinstance(v,bool) else v)' "$SETTINGS" "$1"; }
DELIVER=$(field deliver) || exit 2
RUNTIME=$(field runtime) || exit 2
PROXY=$(field proxy) || exit 2
DEFAULT_FAST=$(field fast) || exit 2
SENSITIVE=$(field sensitive) || exit 2
RUN_DIR="$DELIVER/$TASK_ID/$RUN_ID"
if [ -e "$RUN_DIR" ]; then echo "已存在 ${RUN_DIR}，拒绝重复执行" >&2; exit 3; fi
mkdir -p "$DELIVER/$TASK_ID" || exit 4
mkdir "$RUN_DIR" || exit 4
cp "$TASK_FILE" "$RUN_DIR/任务.txt"   # 留存原始派单，方便事后对账
# 输入文件：派活前设 WORKER_INPUT_DIR=<目录>，把它的内容复制进本轮目录的 输入/（工人只能在本轮目录里读写）
if [ -n "${WORKER_INPUT_DIR:-}" ]; then
  [ -d "$WORKER_INPUT_DIR" ] || { echo "找不到输入目录：$WORKER_INPUT_DIR" >&2; exit 2; }
  mkdir -p "$RUN_DIR/输入" && cp -R "$WORKER_INPUT_DIR"/. "$RUN_DIR/输入/"
fi

# ---------- 3. 网络代理：只给 codex 进程本身连 OpenAI 用（沙箱里的命令本来就断网）----------
# 空配置明确关闭代理，也清掉继承的大小写代理变量。
unset HTTPS_PROXY HTTP_PROXY ALL_PROXY NO_PROXY https_proxy http_proxy all_proxy no_proxy
if [ -n "$PROXY" ]; then
  export HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY" https_proxy="$PROXY" http_proxy="$PROXY"
  export NO_PROXY="localhost,127.0.0.1" no_proxy="localhost,127.0.0.1"
fi
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# 给看板用：记下谁在跑、几点开始、最多跑多久、本脚本的进程号。
# 看板据此判断：有这个文件、还没有回执、进程还在 = 正在干活；进程没了却没有回执 = 中断，要提醒 使用者
printf '{"工人":"codex","模型":"%s","档位":"%s","开始":"%s","超时秒":%s,"进程号":%s}\n' \
  "$MODEL" "$EFFORT" "$START" "$TIMEOUT_SEC" "$$" > "$RUN_DIR/运行信息.json"

# ---------- 4. 严格档沙箱：在默认工作区权限的基础上，禁止读所有存放密钥和登录的位置 ----------
# 用 JSON 字符串转义 TOML 路径；运行时位置随配置变更仍然禁读。
DENY=$(python3 -I - "$RUNTIME" "$SENSITIVE" <<'PYDENY'
import json, sys
paths = ['~/.hermes', '~/.frclyo', '~/.codex', '~/.claude', '~/.ssh', '~/.gnupg',
         '~/.config', '~/.aws', '~/.docker', '~/.netrc', '~/.local/share/opencode',
         '~/Library/Keychains', sys.argv[1]] + json.loads(sys.argv[2])
print(', '.join(json.dumps(p, ensure_ascii=False) + '="deny"' for p in paths))
PYDENY
) || exit 2
PERM="permissions.frclyo_worker={extends=\":workspace\", filesystem={$DENY}}"

# ---------- 5. 子 agent 开关：默认不派（控制用量、行为可预期）；派活方需要时设 CODEX_WORKER_SUBAGENTS=1 ----------
SUBAGENT_FLAG=(--disable multi_agent)
[ "${CODEX_WORKER_SUBAGENTS:-0}" = "1" ] && SUBAGENT_FLAG=()
# 网页搜索开关（使用者 2026-10-13 选"只开网页搜索、按任务开"）：默认关；设 CODEX_WORKER_WEB=1 时打开。
# 打开后模型能用 OpenAI 自带的网页搜索（搜索在 OpenAI 那边做、结果交给模型），沙箱不变，工人跑的命令仍然断网。
# 注意 --search 是 codex 的顶层参数，必须写在 exec 前面（写在后面会报 unexpected argument，2026-10-13 实测）
WEB_TOP=(); WEB_ARGS=(-c 'web_search="disabled"')
[ "${CODEX_WORKER_WEB:-0}" = "1" ] && { WEB_TOP=(--search); WEB_ARGS=(); }
# 快速档：默认开；只有模型清单里写了支持 Fast 档（priority）才加，不支持就照常跑，不报错
FAST_ARGS=()
if [ "${CODEX_WORKER_FAST:-$DEFAULT_FAST}" = "1" ] && python3 -I -c "import json,sys; m=[x for x in json.load(open(sys.argv[2], encoding='utf-8'))['models'] if x.get('slug')==sys.argv[1]]; sys.exit(0 if m and any(t.get('id')=='priority' for t in m[0].get('service_tiers') or []) else 1)" "$MODEL" "$CODEX_CACHE" 2>/dev/null; then
  FAST_ARGS=(-c 'service_tier="priority"')
fi

# ---------- 6. codex exec 的全部参数（数组写法，每项的作用写在旁边）----------
ARGS=(
  ${WEB_TOP[@]+"${WEB_TOP[@]}"}   # 开网页搜索时这里是 --search（顶层参数，必须在 exec 前面）
  exec
  --ephemeral                     # 不在 ~/.codex 留会话（使用者 的决定；记录写在本轮目录里）
  --ignore-user-config            # 不读 ~/.codex/config.toml：不带插件/MCP/记忆/通知/信任表
  --ignore-rules                  # 不读 ~/.codex/rules（曾有让 cp 绕过沙箱的放行规则）
  --disable hooks                 # 不跑任何钩子
  --disable memories              # 不读也不写 使用者 的共享记忆
  --disable apps                  # 不暴露 ChatGPT 连接器（使用者 的各类账号）
  --disable plugins               # 不加载插件
  --disable computer_use --disable browser_use --disable browser_use_external  # 不能操控桌面和浏览器
  ${SUBAGENT_FLAG[@]+"${SUBAGENT_FLAG[@]}"}   # 写成这样：开子 agent 时数组为空，bash 3.2 直接写 "${A[@]}" 会报错退出
  -C "$RUN_DIR"                   # 工作根目录 = 本轮专属目录
  -c "$PERM"                      # 定义严格档
  -c 'default_permissions="frclyo_worker"'               # 启用严格档（不能再传 -s，否则严格档被忽略）
  -c 'approvals_reviewer="user"'  # 不允许被切成"模型替人审批"
  -c 'approval_policy="never"'    # 越界请求一律拒绝并交回模型，不会卡住等人
  -c 'shell_environment_policy.inherit="core"'          # 命令只拿到 PATH/HOME/LANG 等基础变量
  -c 'shell_environment_policy.ignore_default_excludes=false'  # 再滤掉名字含 KEY/SECRET/TOKEN 的变量
  -c 'history.persistence="none"' # 不写 ~/.codex/history.jsonl
  ${WEB_ARGS[@]+"${WEB_ARGS[@]}"}  # 网页搜索：默认关，CODEX_WORKER_WEB=1 才开（见上面）
  ${FAST_ARGS[@]+"${FAST_ARGS[@]}"}  # 快速档：默认开，CODEX_WORKER_FAST=0 关（见上面）
  -m "$MODEL"                     # 模型由派活方按任务给（上面已对照清单检查）
  -c "model_reasoning_effort=\"$EFFORT\""                # 思考档位由派活方按任务给
  --skip-git-repo-check           # 交付目录在 git 仓库里也可能不在，统一跳过检查
  --json                          # 标准输出是 JSONL 事件流
  -o "$RUN_DIR/结果.txt"           # 最后一条回复写进 结果.txt
  -                               # 任务说明从标准输入读（避免引号问题）
)
export CODEX_WORKER_FAST_ON=$([ ${#FAST_ARGS[@]} -gt 0 ] && echo 1 || echo 0)   # 给回执记"这次开没开快速档"
codex "${ARGS[@]}" < "$RUN_DIR/任务.txt" > "$RUN_DIR/events.jsonl" 2> "$RUN_DIR/stderr.log" &
CPID=$!

# ---------- 7. 超时看门狗：codex exec 没有总时长上限，到点先温和终止，10 秒后还在就强制结束 ----------
( sleep "$TIMEOUT_SEC"; touch "$RUN_DIR/.超时"; kill -TERM "$CPID" 2>/dev/null; sleep 10; kill -KILL "$CPID" 2>/dev/null ) >/dev/null 2>&1 &
WPID=$!
disown "$WPID" 2>/dev/null       # 让 shell 不再跟踪看门狗，结束它时不会打印 "Terminated" 提示
wait "$CPID"; RC=$?
pkill -P "$WPID" 2>/dev/null; kill "$WPID" 2>/dev/null   # codex 正常结束后，收掉看门狗和它的 sleep
# Codex 被终止时可能"正常退出"返回 0，所以以看门狗留下的标记为准：有标记就是超时，退出码统一定为 124
if [ -e "$RUN_DIR/.超时" ]; then RC=124; rm -f "$RUN_DIR/.超时"; fi
END=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# ---------- 8. 写回执：只写机器能核对的事实，不写"已收到/验收通过"（那不归执行方判断）----------
python3 -I - "$RUN_DIR" "$TASK_ID" "$RUN_ID" "$RC" "$START" "$END" "$EFFORT" "$MODEL" "$BIN_DIR" "$CLI_VER" <<'EOF'
import json, sys, hashlib, os
d, task, run, rc, st, en, effort, model = sys.argv[1:9]
sys.path.insert(0, sys.argv[9])
from frclyo.models import observe
version = sys.argv[10] or 'unknown'
tid, usage, cmds, completed = None, None, 0, False
try:
    for line in open(os.path.join(d, 'events.jsonl'), encoding='utf-8'):
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get('type')
        if t == 'thread.started':
            tid = e.get('thread_id')
        elif t == 'turn.completed':
            completed = True
            usage = e.get('usage')            # 本轮 token 用量，用于以后比较各工人表现
        elif t == 'turn.failed':
            completed = False
        elif t == 'item.completed' and (e.get('item') or {}).get('type') == 'command_execution':
            cmds += 1                         # 执行过的命令数
except FileNotFoundError:
    pass
res = os.path.join(d, '结果.txt')
sha = hashlib.sha256(open(res, 'rb').read()).hexdigest() if os.path.exists(res) else None
ok = (rc == '0' and sha is not None and completed)
availability = observe('codex', model, version, d, succeeded=ok)
if availability == 'unusable':
    ok = False
if not ok and rc == '0':
    rc = '1'
json.dump({'工人': 'codex', '模型': model, '思考档位': effort,
           'CLI版本': version, '模型可用性': availability,
           '网页搜索': os.environ.get('CODEX_WORKER_WEB', '0') == '1',
           '快速档': os.environ.get('CODEX_WORKER_FAST_ON', '0') == '1',
           '任务编号': task, '轮次编号': run, '退出码': int(rc), 'threadId': tid,
           '开始': st, '结束': en, '执行命令数': cmds, '用量': usage,
           '结果sha256': sha, '状态': '执行完成' if ok else ('超时' if rc == '124' else '失败')},
          open(os.path.join(d, '回执.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
sys.exit(0 if ok else 1)
EOF
# CLI 即使错误地返回 0，明确的模型拒绝或记录失败也不能报告成功。
RECEIPT_RC=$?
if [ "$RC" = "0" ] && [ "$RECEIPT_RC" != "0" ]; then RC=$RECEIPT_RC; fi
exit "$RC"

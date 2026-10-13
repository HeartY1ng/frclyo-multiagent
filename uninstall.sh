#!/bin/bash
# 卸载共用 aw 的安装记录解析，避免两套路径判断不一致。
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec bash "$ROOT/bin/aw" __uninstall "$@"

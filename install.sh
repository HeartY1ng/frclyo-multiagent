#!/bin/bash
# 安装器只定位仓库；交互、记录和撤销逻辑统一由 aw 实现。
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec bash "$ROOT/bin/aw" __install "$@"

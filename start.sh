#!/usr/bin/env bash
# ============================================================
#  cangyao QQ 机器人 - Linux/Ubuntu 启动脚本
#  用法: ./start.sh
#  后台运行: nohup ./start.sh > /dev/null 2>&1 &
# ============================================================

set -e

# 切换到脚本所在目录(项目根目录)
cd "$(dirname "$0")"

# UTF-8 编码，防止中文乱码
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

# 使用虚拟环境中的 Python 启动
if [[ ! -x ".venv/bin/python" ]]; then
    echo "[错误] 未找到虚拟环境 .venv，请先运行: sudo bash install_ubuntu.sh" >&2
    exit 1
fi

exec .venv/bin/python bot.py

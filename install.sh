#!/usr/bin/env sh
# coara 一键安装：建虚拟环境、装依赖、跑一遍冒烟测试。
set -e

PY="${PYTHON:-python3}"

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "没找到 $PY。请先装 Python 3.11 或更高版本。" >&2
  exit 1
fi

"$PY" -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"

echo
echo "装好了。"
echo "  coara          拉起内核并接入终端"
echo "  coara status   看运行时状态"
echo "  pytest tests/ -q   跑一遍测试"

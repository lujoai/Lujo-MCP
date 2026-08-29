#!/bin/bash
set -e

echo "Running lint checks..."

if command -v ruff &> /dev/null; then
    ruff check .
else
    echo "ruff not found, installing..."
    # FIX(v0.7.0 Minor): 锁定精确版本，根治"本地绿/CI 红"——ruff 每个 minor
    # 都可能向默认规则集加规则，裸 install 拿到更新版本即多报规则。
    # 版本与 requirements-dev.txt（ruff>=0.16.4,<0.17.0）同源，升级时两处同步改。
    pip install "ruff==0.16.4"
    ruff check .
fi

echo ""
echo "Lint checks completed."
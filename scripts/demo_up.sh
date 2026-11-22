#!/usr/bin/env bash
# 本地启动两个演示服务（只绑 127.0.0.1）。用法：scripts/demo_up.sh [--no-llm]
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p var
for spec in "agentpay 8702" "gouji 8703"; do
  set -- $spec
  if [ -f "var/$1.pid" ] && kill -0 "$(cat var/$1.pid)" 2>/dev/null; then echo "$1 已在运行 pid=$(cat var/$1.pid)"; continue; fi
  nohup .venv/bin/python -m demo.run "$1" --port "$2" ${NO_LLM:+--no-llm} > "var/$1.log" 2>&1 &
  echo $! > "var/$1.pid"
  echo "$1 → http://127.0.0.1:$2  pid=$!"
done
for port in 8702 8703; do
  for _ in $(seq 1 50); do curl -fsS "http://127.0.0.1:$port/static/app.css" >/dev/null 2>&1 && break; sleep 0.2; done
done

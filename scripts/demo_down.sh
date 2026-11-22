#!/usr/bin/env bash
# 按 PID 停掉演示服务。
cd "$(dirname "$0")/.."
for n in agentpay gouji; do
  if [ -f "var/$n.pid" ]; then pid=$(cat "var/$n.pid"); kill "$pid" 2>/dev/null && echo "stopped $n pid=$pid"; rm -f "var/$n.pid"; fi
done

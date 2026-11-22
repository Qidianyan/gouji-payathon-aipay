#!/usr/bin/env bash
# 浏览器 e2e：重启两个演示服务（规则选品，确定性）→ playwright 跑全部按钮/分享链路 → 截图 → 按 PID 停服务。
set -uo pipefail
cd "$(dirname "$0")/.."
scripts/demo_down.sh >/dev/null 2>&1
NO_LLM=1 scripts/demo_up.sh
(cd web/e2e && node e2e.mjs); rc=$?
scripts/demo_down.sh
exit $rc

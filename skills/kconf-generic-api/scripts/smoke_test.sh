#!/usr/bin/env bash
# ConfigService generic-api 冒烟测试：只跑只读 + 无副作用接口，写操作（update/gray/rollback/review-*）只验证前置链路
set -u
SKILL_DIR="/data/aime/0874fe2a-5050-414b-93da-cb896e173a71/workspace/user-skills/kconf-generic-api"
CLI="$SKILL_DIR/scripts/kconf_client.py"
K="ad.demo.chainGrayConfig"
RESULT_FILE="/tmp/kconf_smoke_results.jsonl"
: > "$RESULT_FILE"

run() {
  local name="$1"; shift
  local cli="$1"; shift
  local out
  out=$(uv run "$cli" "$@" 2>&1 | tail -1)
  local ok code err
  ok=$(echo "$out" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('ok'))" 2>/dev/null || echo parse_error)
  code=$(echo "$out" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('code'))" 2>/dev/null || echo parse_error)
  err=$(echo "$out" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('error') or '')" 2>/dev/null || echo parse_error)
  echo "{\"name\":\"$name\",\"ok\":\"$ok\",\"code\":\"$code\",\"error\":\"$err\"}" >> "$RESULT_FILE"
  printf "%-32s ok=%s\n" "$name" "$ok"
}

# 先取依赖数据（get / history-list），后面用例要引用
GET_OUT=$(uv run "$CLI" get --key "$K" 2>&1 | tail -1)
SNAPSHOT_ID=$(echo "$GET_OUT" | python3 -c "import json,sys;d=json.load(sys.stdin);subs=d['data']['subConfigs'];print(next(s['curConfig']['snapshotId'] for s in subs if s.get('stage')=='production'))" 2>/dev/null)
INSPECT_ID=$(echo "$GET_OUT" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['data'].get('inspectionTaskId') or '')" 2>/dev/null)
HIST_OUT=$(uv run "$CLI" history-list --key "$K" --size 5 2>&1 | tail -1)
LATEST_VERSION=$(echo "$HIST_OUT" | python3 -c "import json,sys;d=json.load(sys.stdin);c=d['data'].get('content') or [];print(c[0]['version'] if c else '')" 2>/dev/null)

echo "=== 场景一：找配置 ==="
run "dir-path" "$CLI" dir-path
run "dir-list" "$CLI" dir-list --dir ad --size 3
run "dir-get" "$CLI" dir-get --dir ad
run "dir-search" "$CLI" dir-search --path adtarget
run "dir-search-first" "$CLI" dir-search-first --path ad
run "config-lite-list" "$CLI" config-lite-list --dir ad.demoTarget --size 3
run "config-list" "$CLI" config-list --dir ad.demoTarget --size 2
run "config-list-keys" "$CLI" config-list-keys --dir ad.demoTarget
sleep 2
run "search-config" "$CLI" search-config --keyword chainGrayConfig
run "search-config-legacy" "$CLI" search-config-legacy --keyword chainGrayConfig

echo "=== 场景二：查配置 ==="
run "islogin" "$CLI" islogin
run "get" "$CLI" get --key "$K"
run "get-stage" "$CLI" get --key "$K" --stage production
run "get-legacy" "$CLI" get-legacy --key "$K"
run "schema-get" "$CLI" schema-get --key "$K"
run "maintenance" "$CLI" maintenance --key "$K"
run "service-level-count" "$CLI" service-level-count --key "$K"
run "stability-result" "$CLI" stability-result --inspection-task-id "$INSPECT_ID" --key "$K" --env PROD
run "direct-change-check" "$CLI" direct-change-check --key "$K"
run "precheck" "$CLI" precheck --key "$K" --stage production

echo "=== 场景三：写前置链路（不真写生产） ==="
run "get-reviewers" "$CLI" get-reviewers --key "$K"

echo "=== 场景五：历史与监控 ==="
run "history-list" "$CLI" history-list --key "$K" --size 5
run "history-detail" "$CLI" history-detail --key "$K" --version "$LATEST_VERSION"
run "history-version" "$CLI" history-version --key "$K"
run "history-change-env" "$CLI" history-change-env --key "$K"
run "snapshot-get" "$CLI" snapshot-get --id "$SNAPSHOT_ID"
run "monitor-ksn" "$CLI" monitor-ksn --key "$K"
run "monitor-machine" "$CLI" monitor-machine --key "$K"

echo "=== 场景六：管理 ==="
run "get-manage-data" "$CLI" get-manage-data --key "$K"
run "favorite-list" "$CLI" favorite-list
run "visited-list" "$CLI" visited-list
run "last-modified" "$CLI" last-modified
run "pending-count" "$CLI" pending-count
run "alertbox" "$CLI" alertbox

echo "=== 批量：guard 与幂等回查（不真跑批量写） ==="
GUARD_OUT=$(uv run "$SKILL_DIR/scripts/kconf_batch.py" check-action --key "$K" --action gray-next --params-file /dev/null 2>&1 | tail -1)
echo "$GUARD_OUT" | head -c 200; echo
echo '{}' > /tmp/kconf_empty_params.json
run "batch-check-action" "$SKILL_DIR/scripts/kconf_batch.py" check-action --key "$K" --action gray-next --params-file /tmp/kconf_empty_params.json

echo "=== 汇总 ==="
python3 - "$RESULT_FILE" <<'PY'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
# guard 生效（REMOTE_AGENT_REQUIRED）与成功均视为通过；parse_error 视为失败
passed = [r for r in rows if r["ok"] == "True" or r["error"] == "REMOTE_AGENT_REQUIRED"]
bad = [r["name"] for r in rows if r["ok"] != "True" and r["error"] != "REMOTE_AGENT_REQUIRED"]
guards = [r["name"] for r in rows if r["error"] == "REMOTE_AGENT_REQUIRED"]
print(f"TOTAL={len(rows)} PASS={len(passed)} FAIL={len(bad)}")
if guards:
    print("GUARD_EXPECTED:", ", ".join(guards))
if bad:
    print("FAILED:", ", ".join(bad))
    sys.exit(1)
print("ALL PASSED")
PY

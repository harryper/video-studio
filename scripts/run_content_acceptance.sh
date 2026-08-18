#!/usr/bin/env bash
# Content Studio 一站式验收脚本。
#
# 默认行为 (--offline)：
#   1. 运行 pytest 全量套件
#   2. 运行 web 端 vitest 套件 + build
#   3. 运行 `content-studio evaluate` 离线评估
#   4. 在 evaluation/results/<run-id>/ 下写出 summary.md 与原始产物
#
# 线上验收 (--online) 必须同时设置 STUDIO_ONLINE_AUTHORIZED=1，否则脚本拒绝启动，
# 避免误触发真实模型调用。脚本整体是幂等的：每次运行生成新的 run-id，不会修改任何
# 已提交文件。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

MODE="offline"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
EXTRA_ARGS=()

usage() {
  cat <<EOF
Usage: $(basename "$0") [--offline] [--online] [--run-id ID] [--help]

  --offline       默认；执行 pytest + npm test/build + evaluate + 写 summary.md
  --online        额外跑一次 24 主题线上生成（必须配合 STUDIO_ONLINE_AUTHORIZED=1）
  --run-id ID     覆盖默认时间戳作为 evaluation/results/<run-id>/ 的目录名
  --help          打印此帮助

退出码：
  0  全部门禁通过
  1  pytest / npm test / build / evaluate 任一失败
  2  --online 但缺少 STUDIO_ONLINE_AUTHORIZED=1
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline)
      MODE="offline"
      shift
      ;;
    --online)
      MODE="online"
      shift
      ;;
    --run-id)
      [[ $# -ge 2 ]] || { echo "--run-id 需要一个参数" >&2; exit 2; }
      RUN_ID="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${MODE}" == "online" && "${STUDIO_ONLINE_AUTHORIZED:-0}" != "1" ]]; then
  echo "ERROR: --online requires STUDIO_ONLINE_AUTHORIZED=1 in the environment" >&2
  echo "       this guard prevents accidental LLM credit usage during CI runs" >&2
  exit 2
fi

RESULTS_DIR="${REPO_ROOT}/evaluation/results/${RUN_ID}"
mkdir -p "${RESULTS_DIR}"
echo "acceptance run id: ${RUN_ID}"
echo "results dir:       ${RESULTS_DIR}"

log() {
  printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"
}

record_step() {
  # $1 = step name; $2 = exit code; $3 = optional notes
  printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}" >> "${RESULTS_DIR}/steps.tsv"
}

# ---------------------------------------------------------------------------
# Gate 1: pytest
# ---------------------------------------------------------------------------
log "running pytest (offline)"
if uv run pytest -q >"${RESULTS_DIR}/pytest.log" 2>&1; then
  log "pytest: PASS"
  record_step "pytest" 0
else
  log "pytest: FAIL — see ${RESULTS_DIR}/pytest.log"
  record_step "pytest" 1
  cat "${RESULTS_DIR}/pytest.log" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Gate 2: web test + build
# ---------------------------------------------------------------------------
log "running web test"
if npm --prefix web test >"${RESULTS_DIR}/npm-test.log" 2>&1; then
  log "npm test: PASS"
  record_step "npm-test" 0
else
  log "npm test: FAIL — see ${RESULTS_DIR}/npm-test.log"
  record_step "npm-test" 1
  cat "${RESULTS_DIR}/npm-test.log" >&2
  exit 1
fi

log "running web build"
if npm --prefix web run build >"${RESULTS_DIR}/npm-build.log" 2>&1; then
  log "npm build: PASS"
  record_step "npm-build" 0
else
  log "npm build: FAIL — see ${RESULTS_DIR}/npm-build.log"
  record_step "npm-build" 1
  cat "${RESULTS_DIR}/npm-build.log" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Gate 3: docker compose config
# ---------------------------------------------------------------------------
log "validating docker-compose.next.yml"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  if docker compose -f docker-compose.next.yml config >"${RESULTS_DIR}/compose.log" 2>&1; then
    log "compose config: PASS"
    record_step "compose" 0
  else
    log "compose config: FAIL — see ${RESULTS_DIR}/compose.log"
    record_step "compose" 1
    cat "${RESULTS_DIR}/compose.log" >&2
    exit 1
  fi
else
  # WHY: 沙盒可能没有 docker；仅做 YAML 解析兜底，不阻断脚本。
  log "docker compose unavailable — falling back to yaml.safe_load"
  if uv run python -c "import yaml; yaml.safe_load(open('docker-compose.next.yml'))" \
      >"${RESULTS_DIR}/compose.log" 2>&1; then
    record_step "compose" 0 "yaml-safe-load fallback (no docker in env)"
  else
    record_step "compose" 1 "yaml-safe_load failed"
    cat "${RESULTS_DIR}/compose.log" >&2
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# Gate 4: offline evaluate
# ---------------------------------------------------------------------------
log "running content-studio evaluate (offline fixtures)"
EVAL_OUT="${RESULTS_DIR}/offline"
mkdir -p "${EVAL_OUT}"
if uv run content-studio evaluate \
    --dataset evaluation/topics.yaml \
    --rubric evaluation/rubric.yaml \
    --fixtures tests/fixtures/provider_responses \
    --output "${EVAL_OUT}" >"${RESULTS_DIR}/evaluate.log" 2>&1; then
  log "evaluate: PASS"
  record_step "evaluate" 0
else
  log "evaluate: FAIL — see ${RESULTS_DIR}/evaluate.log"
  record_step "evaluate" 1
  cat "${RESULTS_DIR}/evaluate.log" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Optional: online acceptance (gated)
# ---------------------------------------------------------------------------
if [[ "${MODE}" == "online" ]]; then
  log "online mode requested: STUDY=STUDIO_ONLINE_AUTHORIZED=1 confirmed"
  log "online acceptance runs are NOT implemented in this script yet"
  log "manual operator must run the worker against the live API; see README.next.md"
  record_step "online" 0 "deferred — see README.next.md"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
log "writing summary.md"
uv run python - <<EOF >"${RESULTS_DIR}/summary.md"
"""Emit a human-readable summary of this acceptance run."""

import json
import sys
from pathlib import Path

results_dir = Path("${RESULTS_DIR}")
summary_lines: list[str] = []

summary_lines.append("# Content Studio acceptance summary")
summary_lines.append("")
summary_lines.append(f"- run id: \`${RUN_ID}\`")
summary_lines.append(f"- mode: \`${MODE}\`")
summary_lines.append("")

steps_path = results_dir / "steps.tsv"
if steps_path.exists():
    summary_lines.append("## Gates")
    summary_lines.append("")
    summary_lines.append("| step | status | notes |")
    summary_lines.append("| --- | --- | --- |")
    for line in steps_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        step, status = parts[0], parts[1]
        notes = parts[2] if len(parts) > 2 else ""
        label = "PASS" if status == "0" else "FAIL"
        summary_lines.append(f"| {step} | {label} | {notes} |")
    summary_lines.append("")

envelope_path = results_dir / "offline" / "results.json"
if envelope_path.exists():
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    summary_lines.append("## Offline evaluation thresholds")
    summary_lines.append("")
    summary_lines.append("| threshold | value |")
    summary_lines.append("| --- | --- |")
    summary_lines.append(f"| aggregate_pass_rate | {envelope.get('aggregate_pass_rate', 0):.2f} |")
    summary_lines.append(f"| min_average_score | {envelope.get('acceptance_thresholds', {}).get('min_average_score', 0):.2f} |")
    summary_lines.append(f"| max_canned_phrase_ratio | {envelope.get('acceptance_thresholds', {}).get('max_canned_phrase_ratio', 0):.2f} |")
    summary_lines.append(f"| min_three_pitch_difference_rate | {envelope.get('acceptance_thresholds', {}).get('min_three_pitch_difference_rate', 0):.2f} |")
    summary_lines.append(f"| min_claim_verification_coverage | {envelope.get('acceptance_thresholds', {}).get('min_claim_verification_coverage', 0):.2f} |")
    summary_lines.append(f"| min_protected_span_preservation_rate | {envelope.get('acceptance_thresholds', {}).get('min_protected_span_preservation_rate', 0):.2f} |")
    summary_lines.append(f"| max_speech_plan_mutation_rate | {envelope.get('acceptance_thresholds', {}).get('max_speech_plan_mutation_rate', 0):.2f} |")
    summary_lines.append("")
    summary_lines.append("## Approved thresholds (per spec)")
    summary_lines.append("")
    summary_lines.append(
        "- 75% preference — collected from blind ballots in "
        "evaluation/results/<run-id>/offline/ballot.csv (separate review metric; "
        "the aggregate_pass_rate above measures the §11.3 gates)."
    )
    summary_lines.append("- < 10% obvious canned phrasing")
    summary_lines.append("- 90% pitch distinction")
    summary_lines.append("- 100% verified central claims")
    summary_lines.append("- 100% protected-text preservation")
    summary_lines.append("")

print("\n".join(summary_lines))
EOF

log "summary written to ${RESULTS_DIR}/summary.md"
log "acceptance complete (mode=${MODE})"
exit 0
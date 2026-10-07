#!/usr/bin/env bash
# HF Automation health check — zero-LLM cron script.
# Counts runs since service restart. Delivers verbatim via no_agent cron.
set -euo pipefail

NOW=$(date '+%Y-%m-%d %H:%M %Z')
REPORT_DIR="/root/atlas/huggingface/data/health_reports"
mkdir -p "$REPORT_DIR"
REPORT_FILE="$REPORT_DIR/$(date '+%Y-%m-%d_%H%M').txt"

SVC_STATUS=$(systemctl is-active huggingface-automation.service 2>/dev/null || echo "unknown")
SVC_SINCE_RAW=$(systemctl show huggingface-automation.service -p ActiveEnterTimestamp --value 2>/dev/null)
SVC_SINCE_FMT=$(date -d "$SVC_SINCE_RAW" '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo "unknown")
MEM_CURRENT=$(systemctl show huggingface-automation.service -p MemoryCurrent --value 2>/dev/null)
MEM_PEAK=$(systemctl show huggingface-automation.service -p MemoryPeak --value 2>/dev/null)

count_runs() {
    local pattern="$1"
    local since="$2"
    local result
    # Match "Run #N SUCCESS" / "Run #N FAILED" exactly — avoids inflating counts
    # by matching "=== SUCCESS ===" banners or cumulative counter lines.
    result=$(journalctl --no-pager -u huggingface-automation.service --output=cat --since "$since" 2>/dev/null | grep -c "Run #[0-9]\+ ${pattern}" 2>/dev/null) || result=0
    if [ -z "$result" ]; then result=0; fi
    echo "$result"
}

if [ -n "$SVC_SINCE_FMT" ] && [ "$SVC_SINCE_FMT" != "unknown" ]; then
    SUCCESS_COUNT=$(count_runs 'SUCCESS' "$SVC_SINCE_FMT")
    FAIL_COUNT=$(count_runs 'FAILED' "$SVC_SINCE_FMT")
    ERRORS=$(journalctl --no-pager -u huggingface-automation.service --output=cat --since "$SVC_SINCE_FMT" 2>/dev/null | grep 'ERROR during signup' | tail -3 | sed 's/^/  /') || ERRORS=""
else
    SUCCESS_COUNT=0
    FAIL_COUNT=0
    ERRORS=""
fi

CUMULATIVE=$(journalctl --no-pager -u huggingface-automation.service --output=cat 2>/dev/null | grep -oP 'success=\d+\s+fail=\d+' | tail -1) || CUMULATIVE="N/A"

TOTAL=$((SUCCESS_COUNT + FAIL_COUNT))
if [ "$SVC_STATUS" != "active" ]; then
    HEALTH="DEAD"
elif [ "$TOTAL" -eq 0 ]; then
    HEALTH="IDLE (no runs since restart)"
elif [ "$FAIL_COUNT" -eq 0 ]; then
    HEALTH="OK"
elif [ "$FAIL_COUNT" -le "$SUCCESS_COUNT" ]; then
    HEALTH="DEGRADED"
else
    HEALTH="DEGRADED"
fi

mem_fmt() {
    local b=$1
    if [ -z "$b" ] || [ "$b" = "[not set]" ]; then echo "N/A"; return; fi
    if [ "$b" -gt 1073741824 ]; then echo "$((b / 1073741824)).$(( (b % 1073741824) * 10 / 1073741824 ))G"
    elif [ "$b" -gt 1048576 ]; then echo "$((b / 1048576))M"
    else echo "$((b / 1024))K"; fi
}

REPORT=$(cat <<EOF
HF AUTOMATION STATUS — $NOW
Service: $SVC_STATUS | Since: $SVC_SINCE_FMT | Mem: $(mem_fmt "$MEM_CURRENT") (peak $(mem_fmt "$MEM_PEAK"))
Post-fix runs: $SUCCESS_COUNT SUCCESS / $FAIL_COUNT FAILED
Cumulative: $CUMULATIVE
Errors: 
$ERRORS
Health: $HEALTH
EOF
)

echo "$REPORT"
echo "$REPORT" > "$REPORT_FILE"

if [ "$HEALTH" = "OK" ] || [ "$HEALTH" = "IDLE (no runs since restart)" ]; then
    echo "[CLEAN]"
fi

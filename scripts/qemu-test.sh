#!/usr/bin/env bash
#
# Automated offline install test — calls qemu-install.sh with no network
# and monitors the installer log for pass/fail.
#
# Usage:
#   make qemu-test              # use latest ISO
#   TIMEOUT=600 make qemu-test  # custom timeout
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUT_DIR="$PROJECT_DIR/out"
LOG_FILE="$OUT_DIR/qemu-test.log"
TIMEOUT="${TIMEOUT:-900}"

echo "══ Arches Automated Install Test ══"
echo "  Log:     $LOG_FILE"
echo "  Timeout: ${TIMEOUT}s"
echo ""

# Launch qemu-install with no network, fresh disk, and log capture.
# It runs in the background so we can monitor the log.
"$SCRIPT_DIR/qemu-install.sh" \
    --no-network \
    --fresh-disk \
    --log "$LOG_FILE" \
    > /dev/null 2>&1 &
QEMU_PID=$!

cleanup() {
    if [[ -n "${TAIL_PID:-}" ]] && kill -0 "$TAIL_PID" 2>/dev/null; then
        kill "$TAIL_PID" 2>/dev/null || true
    fi
    if kill -0 "$QEMU_PID" 2>/dev/null; then
        kill "$QEMU_PID" 2>/dev/null || true
        wait "$QEMU_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "  QEMU PID: $QEMU_PID"
echo "  Waiting for install to complete..."
echo ""

# Tail the log in real-time
tail -f "$LOG_FILE" 2>/dev/null &
TAIL_PID=$!

# Monitor for completion
START_TIME=$SECONDS
RESULT=""

while true; do
    # Check if QEMU exited
    if ! kill -0 "$QEMU_PID" 2>/dev/null; then
        wait "$QEMU_PID" 2>/dev/null || true
        if [[ -f "$LOG_FILE" ]] && grep -q "== Installation complete ==" "$LOG_FILE" 2>/dev/null; then
            RESULT="pass"
        else
            RESULT="fail"
        fi
        break
    fi

    if [[ -f "$LOG_FILE" ]]; then
        if grep -q "== Installation complete ==" "$LOG_FILE" 2>/dev/null; then
            sleep 10
            if ! kill -0 "$QEMU_PID" 2>/dev/null; then
                RESULT="pass"
            else
                RESULT="pass-no-shutdown"
                kill "$QEMU_PID" 2>/dev/null || true
            fi
            break
        fi

        if grep -q "INSTALL FAILED:" "$LOG_FILE" 2>/dev/null; then
            RESULT="fail"
            sleep 5
            kill "$QEMU_PID" 2>/dev/null || true
            break
        fi
    fi

    ELAPSED=$(( SECONDS - START_TIME ))
    if (( ELAPSED >= TIMEOUT )); then
        RESULT="timeout"
        kill "$QEMU_PID" 2>/dev/null || true
        break
    fi

    sleep 1
done

# Kill tail
if [[ -n "${TAIL_PID:-}" ]] && kill -0 "$TAIL_PID" 2>/dev/null; then
    kill "$TAIL_PID" 2>/dev/null || true
fi
TAIL_PID=""

wait "$QEMU_PID" 2>/dev/null || true
ELAPSED=$(( SECONDS - START_TIME ))

# ── Report ───────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════"

case "$RESULT" in
    pass)
        echo "  PASS — Install completed and VM shut down (${ELAPSED}s)"
        echo "═══════════════════════════════════════════════"
        echo "  Log: $LOG_FILE"
        exit 0
        ;;
    pass-no-shutdown)
        echo "  PASS — Install completed (VM killed) (${ELAPSED}s)"
        echo "═══════════════════════════════════════════════"
        echo "  Log: $LOG_FILE"
        exit 0
        ;;
    fail)
        echo "  FAIL — Install failed (${ELAPSED}s)"
        echo "═══════════════════════════════════════════════"
        echo ""
        if [[ -f "$LOG_FILE" ]]; then
            echo "── Last 50 lines of install log ──"
            tail -50 "$LOG_FILE"
            echo ""
            echo "── Install error context ──"
            grep -A20 "INSTALL FAILED:" "$LOG_FILE" 2>/dev/null || echo "(no INSTALL FAILED marker found)"
        else
            echo "  No install log found — the installer may not have started."
        fi
        echo ""
        echo "  Log: $LOG_FILE"
        exit 1
        ;;
    timeout)
        echo "  FAIL — Timed out after ${TIMEOUT}s"
        echo "═══════════════════════════════════════════════"
        echo ""
        if [[ -f "$LOG_FILE" ]]; then
            echo "── Last 50 lines of install log ──"
            tail -50 "$LOG_FILE"
        else
            echo "  No install log found — the installer may not have started."
        fi
        echo ""
        echo "  Log: $LOG_FILE"
        exit 1
        ;;
esac

#!/bin/bash
set -eu

SRC="${SRC:-239.1.1.1}"
SRC_PORT="${SRC_PORT:-5000}"

IFACE="${IFACE:-}"
IFACE_ARGS=()
if [ -n "$IFACE" ]; then
    IFACE_ARGS=(--iface "$IFACE")
fi

RETRANSMIT_DST="${RETRANSMIT_DST:?RETRANSMIT_DST must be set}"
RETRANSMIT_DST_PORT="${RETRANSMIT_DST_PORT:?RETRANSMIT_DST_PORT must be set}"

SNAPSHOT_API_URL="${SNAPSHOT_API_URL:?SNAPSHOT_API_URL must be set}"
SNAPSHOT_API_TIMEOUT_MS="${SNAPSHOT_API_TIMEOUT_MS:-2000}"
SNAPSHOT_API_TOKEN="${SNAPSHOT_API_TOKEN:-}"
API_TOKEN_ARGS=()
if [ -n "$SNAPSHOT_API_TOKEN" ]; then
    API_TOKEN_ARGS=(--api-token "$SNAPSHOT_API_TOKEN")
fi

# Bez GROUND_SIM_URL Converter bezi dal, jen snimky odejdou pouze s KLV a
# bez PX4 telemetrie - zamerne nepovinne, aby sel otestovat i samotny
# prevod TS -> snimek bez bezicího simulatoru.
GROUND_SIM_URL="${GROUND_SIM_URL:-}"
GROUND_SIM_ARGS=()
if [ -n "$GROUND_SIM_URL" ]; then
    GROUND_SIM_ARGS=(--ground-sim-url "$GROUND_SIM_URL"
                     --ground-sim-uav "${GROUND_SIM_UAV:-uav1}"
                     --ground-sim-poll-s "${GROUND_SIM_POLL_S:-1.0}"
                     --ground-sim-timeout-ms "${GROUND_SIM_TIMEOUT_MS:-1000}"
                     --ground-sim-max-age-s "${GROUND_SIM_MAX_AGE_S:-10}")
fi

# Prazdne = state_holder.py si fps zmeri sam z PTS streamu (FrameRateDetector).
# Vyplnit jen pri potrebe rucne prepsat namerenou hodnotu.
# Druhy cil - zakaznicke API. Prazdne = posila se jen na SNAPSHOT_API_URL.
TARGET_API_URL="${TARGET_API_URL:-}"
TARGET_ARGS=()
if [ -n "$TARGET_API_URL" ]; then
    TARGET_ARGS=(--target-api-url "$TARGET_API_URL"
                   --target-api-timeout-ms "${TARGET_API_TIMEOUT_MS:-2000}")
    if [ -n "${TARGET_API_TOKEN:-}" ]; then
        TARGET_ARGS+=(--target-api-token "$TARGET_API_TOKEN")
    fi
fi

# Ukladani na disk. Prazdne = neuklada se.
SNAPSHOT_SAVE_DIR="${SNAPSHOT_SAVE_DIR:-}"
SAVE_ARGS=()
if [ -n "$SNAPSHOT_SAVE_DIR" ]; then
    SAVE_ARGS=(--save-dir "$SNAPSHOT_SAVE_DIR"
               --save-keep "${SNAPSHOT_SAVE_KEEP:-500}")
fi

# Takt odesilani: kazdych tolik sekund odejde na KAZDY cil prave jedna
# zprava - snimek, nebo heartbeat. Prijemce podle toho pozna vypadek.
SEND_INTERVAL_S="${SEND_INTERVAL_S:-1.0}"
# Prijmovy buffer UDP socketu a horizont cekajicich PTS - ladici parametry,
# vychozi hodnoty staci; menit az kdyz log hlasi ztraty nebo nedohledatelne PTS.
RECV_BUFFER_MB="${RECV_BUFFER_MB:-8}"
PTS_HORIZON_S="${PTS_HORIZON_S:-30}"

INPUT_FPS="${INPUT_FPS:-}"
INPUT_FPS_ARGS=()
if [ -n "$INPUT_FPS" ]; then
    INPUT_FPS_ARGS=(--input-fps "$INPUT_FPS")
fi
JPEG_QUALITY="${JPEG_QUALITY:-2}"

echo "[entrypoint] retransmitter: $SRC:$SRC_PORT -> $RETRANSMIT_DST:$RETRANSMIT_DST_PORT${IFACE:+ (iface $IFACE)}"
python3 /app/retransmitter.py \
    --src "$SRC" --src-port "$SRC_PORT" "${IFACE_ARGS[@]}" \
    --dst "$RETRANSMIT_DST" --dst-port "$RETRANSMIT_DST_PORT" &
RETRANSMITTER_PID=$!

echo "[entrypoint] state_holder: $SRC:$SRC_PORT -> POST $SNAPSHOT_API_URL${TARGET_API_URL:+ + zakaznicke $TARGET_API_URL}${SNAPSHOT_SAVE_DIR:+ + disk $SNAPSHOT_SAVE_DIR} (fps=${INPUT_FPS:-auto})${IFACE:+ (iface $IFACE)}${GROUND_SIM_URL:+ (ground sim $GROUND_SIM_URL, ${GROUND_SIM_UAV:-uav1})}"
python3 /app/state_holder.py \
    --src "$SRC" --src-port "$SRC_PORT" "${IFACE_ARGS[@]}" \
    --api-url "$SNAPSHOT_API_URL" \
    --api-timeout-ms "$SNAPSHOT_API_TIMEOUT_MS" "${API_TOKEN_ARGS[@]}" \
    "${TARGET_ARGS[@]}" "${SAVE_ARGS[@]}" \
    --interval-s "$SEND_INTERVAL_S" \
    --recv-buffer-mb "$RECV_BUFFER_MB" \
    --pts-horizon-s "$PTS_HORIZON_S" \
    "${GROUND_SIM_ARGS[@]}" \
    "${INPUT_FPS_ARGS[@]}" --jpeg-quality "$JPEG_QUALITY" &
STATE_HOLDER_PID=$!

trap 'echo "[entrypoint] stopping both processes"; kill "$RETRANSMITTER_PID" "$STATE_HOLDER_PID" 2>/dev/null' TERM INT

wait -n "$RETRANSMITTER_PID" "$STATE_HOLDER_PID"
EXIT_CODE=$?
echo "[entrypoint] one process exited (code $EXIT_CODE) - stopping the other one too"
kill "$RETRANSMITTER_PID" "$STATE_HOLDER_PID" 2>/dev/null || true
wait "$RETRANSMITTER_PID" "$STATE_HOLDER_PID" 2>/dev/null || true
exit "$EXIT_CODE"

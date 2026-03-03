#!/usr/bin/env bash
set -euo pipefail

DOT_PIOREACTOR="${DOT_PIOREACTOR:-$HOME/.pioreactor}"
ESTIMATOR_NAME="fir_temperature_estimator_linear_mlx_ambient_pcb_v1"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TARGET_DIR="$DOT_PIOREACTOR/storage/estimators/temperature_fir"
TARGET_FILE="$TARGET_DIR/${ESTIMATOR_NAME}.yaml"
SOURCE_FILE="$SCRIPT_DIR/estimators/${ESTIMATOR_NAME}.yaml"

mkdir -p "$TARGET_DIR"
cp "$SOURCE_FILE" "$TARGET_FILE"

echo "Seeded estimator to $TARGET_FILE"

if command -v pio >/dev/null 2>&1; then
  pio estimators set-active --device temperature_fir --name "$ESTIMATOR_NAME"
  echo "Set active estimator for temperature_fir to $ESTIMATOR_NAME"
else
  echo "pio command not found. Run this manually:"
  echo "  pio estimators set-active --device temperature_fir --name $ESTIMATOR_NAME"
fi

sudo systemctl restart pioreactor-web.target || true

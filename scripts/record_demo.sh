
#!/usr/bin/env bash
set -u

# Run from the repository root.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

INCIDENT_ID="${1:-inventory_oversell-latent-s1}"
shift || true

TIMESTAMP="$(date '+%Y%m%d-%H%M%S')"
OUTPUT_DIR="results/demo"
OUTPUT_FILE="${OUTPUT_DIR}/${INCIDENT_ID}-${TIMESTAMP}.txt"

mkdir -p "$OUTPUT_DIR"

echo "Running OpsPilot incident: $INCIDENT_ID"
echo "Saving output to: $OUTPUT_FILE"


set +e
python3 -m opspilot demo "$INCIDENT_ID" "$@" 2>&1 | tee "$OUTPUT_FILE"
STATUS=${PIPESTATUS[0]}
set -e


echo
echo "Demo exit code: $STATUS"
echo "Saved report: $OUTPUT_FILE"

exit "$STATUS"

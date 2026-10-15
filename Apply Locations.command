#!/bin/zsh
cd /Users/mo/photo-edit

echo "=== Apply Locations ==="
echo ""

# Find the most recent approved JSON in Downloads
LATEST=$(ls -t ~/Downloads/approved_*.json 2>/dev/null | head -1)

if [[ -z "$LATEST" ]]; then
  echo "No approved_*.json found in ~/Downloads"
  echo ""
  echo "Press Return to close..."
  read
  exit 1
fi

echo "Found: $LATEST"
echo ""
python3.13 apply_locations.py "$LATEST"
echo ""
echo "Press Return to close..."
read

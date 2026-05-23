#!/bin/bash
# setup-irk.sh — install the phone's LE Local IRK onto the host BLE adapter.
#
# BlueZ does NOT persist this across bluetoothd restarts, so this script runs
# on every boot (via cc-ring-setup.service).
#
# Reads the IRK from /etc/cc-ring/irk (hex, no whitespace, no 0x prefix).
# If the file doesn't exist, exits 0 silently — the service is a no-op until
# secrets are populated.
#
# Required: btmgmt (bluez-utils on Arch).

set -euo pipefail

IRK_FILE="${IRK_FILE:-/etc/cc-ring/irk}"
ADAPTER="${ADAPTER:-hci0}"

if [[ ! -f "$IRK_FILE" ]]; then
    echo "no IRK at $IRK_FILE — skipping (populate this file and re-run)"
    exit 0
fi

irk=$(tr -d '[:space:]' < "$IRK_FILE")
if [[ ! "$irk" =~ ^[0-9a-fA-F]{32}$ ]]; then
    echo "IRK in $IRK_FILE is not 32 hex chars (got $(echo -n "$irk" | wc -c))" >&2
    exit 1
fi

# Per PROTOCOL.md §1.3: power cycle the adapter around the privacy/IRK change.
btmgmt -i "$ADAPTER" power off
btmgmt -i "$ADAPTER" privacy on "$irk"
btmgmt -i "$ADAPTER" power on

echo "IRK installed on $ADAPTER"

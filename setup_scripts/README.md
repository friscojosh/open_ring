# setup_scripts

Host-side BLE pairing automation. Per [PROTOCOL.md §1.3](../PROTOCOL.md) the ring requires **three** secrets to accept a central, and one of them (the host adapter's IRK) is wiped on every `bluetoothd` restart and so must be re-applied at boot.

## What you need from your Android phone (rooted)

Pull these three things once:

```bash
# 1. Bluetooth bonding info (contains LE_KEY_PENC — the LTK — and LE_LOCAL_KEY_IRK)
adb shell "su -c 'cat /data/misc/bluetooth/bt_config.conf'" > bt_config.conf

# 2. Oura app's local DB (contains the application-layer auth_key)
adb shell "su -c 'cat /data/data/com.ouraring.oura/files/assa-store.realm'" > assa-store.realm
```

From `bt_config.conf` you'll extract:
- **`LE_LOCAL_KEY_IRK`** under `[Adapter]` — 32 hex chars
- **`LE_KEY_PENC`** under `[<ring-MAC>]` — the LTK + EDIV + Rand bundle (for BlueZ bonding import; not handled by this script)

## Files in this directory

| File | Purpose |
|---|---|
| `setup-irk.sh` | Reads `/etc/cc-ring/irk`, calls `btmgmt power off / privacy on <irk> / power on` on `hci0`. Idempotent. No-op if the IRK file is missing. |
| `cc-ring-setup.service` | systemd oneshot, runs after `bluetooth.service`. Wired into `bluetooth.service.wants/` by `install.sh`. |
| `install.sh` | Copies script into `/usr/local/bin/`, unit into `/etc/systemd/system/`, enables it. |

## Install

```bash
sudo ./install.sh
echo "<your-32-hex-IRK>" | sudo tee /etc/cc-ring/irk
sudo chmod 600 /etc/cc-ring/irk
sudo systemctl start cc-ring-setup.service
sudo systemctl status cc-ring-setup.service     # confirm "IRK installed on hci0"
```

On every boot from then on, the IRK is re-applied automatically.

## Still TODO (by you, manually for now)

1. **Import LTK bonding** into BlueZ. BlueZ stores bonded devices under `/var/lib/bluetooth/<adapter_mac>/<device_mac>/info`. The LTK from `bt_config.conf` `LE_KEY_PENC` has to be byte-swapped and written into the right keys. The author of `open_ring` is intending to ship a helper for this; until then, see [PROTOCOL.md §1.3](../PROTOCOL.md) "Encryption Change Failed" troubleshooting and BlueZ source for the info file format.
2. **Place `auth_key`** at the path your driver invocation expects (`--realm assa-store.realm` accepts the raw realm DB; the driver scans for the marker byte sequence `41 41 41 41 11 00 00 10` and reads the next 16 bytes).

## Why a system service and not a chezmoi-tracked user unit

`btmgmt` requires CAP_NET_ADMIN — it doesn't work as a user systemd service. The IRK itself is a per-device secret that shouldn't be in any dotfiles repo (public or private). Keeping it at `/etc/cc-ring/irk` mode 0600 owned by root keeps the trust scope tight.

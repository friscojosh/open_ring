#!/bin/bash
# install.sh — install setup-irk.sh + systemd unit to system paths.
#
# After: populate /etc/cc-ring/irk with the 32-hex-char IRK from your phone's
# bt_config.conf [Adapter] LE_LOCAL_KEY_IRK, then `systemctl start cc-ring-setup`.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must run as root (try: sudo ./install.sh)" >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 0755 "$HERE/setup-irk.sh" /usr/local/bin/cc-ring-setup-irk
install -m 0644 "$HERE/cc-ring-setup.service" /etc/systemd/system/cc-ring-setup.service
install -d -m 0700 /etc/cc-ring

systemctl daemon-reload
systemctl enable cc-ring-setup.service

echo
echo "Installed. Next steps:"
echo "  echo '<your-32-hex-IRK>' | sudo tee /etc/cc-ring/irk"
echo "  sudo chmod 600 /etc/cc-ring/irk"
echo "  sudo systemctl start cc-ring-setup.service"

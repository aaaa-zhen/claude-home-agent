#!/bin/bash
# weixin-agent VPS deployment setup
# Usage: sudo bash setup.sh
set -e

INSTALL_DIR="/opt/weixin-agent"
SERVICE_USER="weixin"

echo "=== weixin-agent VPS Setup ==="

# 1. Create service user (no login shell)
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd -r -s /bin/bash -m "$SERVICE_USER"
    echo "Created user: $SERVICE_USER"
fi

# 2. Install system dependencies
echo "Installing dependencies..."
apt-get update -qq
apt-get install -y -qq nodejs npm python3 python3-pip python3-venv

# 3. Setup project directory
mkdir -p "$INSTALL_DIR/memory" "$INSTALL_DIR/tools"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"

# 4. Install Python dependencies
sudo -u "$SERVICE_USER" pip3 install --user requests paramiko python-dotenv

# 5. Install weixin-acp
sudo -u "$SERVICE_USER" bash -c "cd $INSTALL_DIR && npm install weixin-acp"

# 6. Install systemd services
cp weixin-agent.service /etc/systemd/system/
cp weixin-monitor.service /etc/systemd/system/
cp weixin-session-manager.service /etc/systemd/system/
systemctl daemon-reload

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy project files to $INSTALL_DIR/"
echo "  2. Copy .env to $INSTALL_DIR/.env and update values"
echo "  3. Download feishu-cli Linux binary to $INSTALL_DIR/tools/"
echo "  4. Enable and start services:"
echo "     systemctl enable --now weixin-agent"
echo "     systemctl enable --now weixin-monitor"
echo "     systemctl enable --now weixin-session-manager"
echo ""
echo "View logs:"
echo "  journalctl -u weixin-agent -f"
echo "  journalctl -u weixin-monitor -f"
echo "  journalctl -u weixin-session-mgr -f"

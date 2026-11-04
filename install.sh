#!/usr/bin/env bash
# 把 netconsole 发送端安装成 systemd 服务（Ubuntu 24.04 / 22.04 通用）。
# 用法： sudo ./install.sh <接收端IP> [端口] [systemd单元...]
# 例：   sudo ./install.sh 192.168.50.164
#        sudo ./install.sh 192.168.50.164 52221 nginx ssh
set -euo pipefail

if [[ $# -lt 1 ]]; then
    cat <<'EOF'
用法: sudo ./install.sh <接收端IP> [端口] [systemd单元...]

  <接收端IP>       必填，运行 netconsole_receiver.py 的那台机器
  [端口]           可选，默认 52221
  [systemd单元]    可选，只转发这些单元的日志；不写则转发全部 journal

示例:
  sudo ./install.sh 192.168.50.164
  sudo ./install.sh 192.168.50.164 52221 nginx ssh
EOF
    exit 1
fi

if [[ $EUID -ne 0 ]]; then
    echo "[!] 请用 sudo 运行" >&2
    exit 1
fi

HOST=$1
shift
PORT=52221
if [[ $# -gt 0 && $1 =~ ^[0-9]+$ ]]; then
    PORT=$1
    shift
fi
UNITS="$*"

if ! [[ $HOST =~ ^[A-Za-z0-9._:-]+$ ]]; then
    echo "[!] 接收端地址看起来不合法：$HOST" >&2
    exit 1
fi

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

install -d -m 0755 /usr/local/share/netconsole
install -m 0755 "$HERE/netconsole_sender.py" /usr/local/bin/netconsole_sender.py
install -m 0644 "$HERE/README.md" /usr/local/share/netconsole/README.md

UNIT_ARGS=""
for unit in $UNITS; do
    UNIT_ARGS="$UNIT_ARGS $unit"
done

sed -e "s|@HOST@|$HOST|g" \
    -e "s|@PORT@|$PORT|g" \
    -e "s|@UNITS@|$UNIT_ARGS|g" \
    "$HERE/netconsole-sender.service.in" > /etc/systemd/system/netconsole-sender.service

systemctl daemon-reload
systemctl enable --now netconsole-sender.service

echo
echo "[+] 已安装并启动：netconsole-sender.service"
echo "    接收端：$HOST:$PORT"
echo "    数据源：journalctl -f -o short-iso${UNIT_ARGS:+ -u$UNIT_ARGS}"
echo
echo "    查看状态： systemctl status netconsole-sender --no-pager"
echo "    查看输出： journalctl -u netconsole-sender -f"
echo "    改配置：   sudoedit /etc/systemd/system/netconsole-sender.service && sudo systemctl daemon-reload && sudo systemctl restart netconsole-sender"

#!/bin/sh
# P1Pro 控制台 · 飞牛版 —— 裸机安装脚本（不用 Docker 的走这条）
#
#   sudo sh install.sh /vol1/1000/apps/p1pro
#
# 做了什么：
#   1. 检查 python3
#   2. 首次运行从 config.example.json 复制一份 config.json
#   3. 写 /etc/systemd/system/p1pro.service 并 enable + start
#
# 卸载：systemctl disable --now p1pro && rm /etc/systemd/system/p1pro.service

set -e

APP_DIR="${1:-$(cd "$(dirname "$0")" && pwd)}"

if [ ! -f "$APP_DIR/app/server/app.py" ]; then
  echo "错误：$APP_DIR 里没有 app/server/app.py，请把项目整个拷过去再运行本脚本" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：找不到 python3。飞牛基于 Debian，执行 apt install -y python3 即可" >&2
  exit 1
fi

PY="$(command -v python3)"
echo "使用解释器：$PY ($("$PY" -V 2>&1))"

if [ ! -f "$APP_DIR/config.json" ]; then
  cp "$APP_DIR/config.example.json" "$APP_DIR/config.json"
  echo "已生成 $APP_DIR/config.json，记得把 device.host 改成你的插座 IP"
fi

mkdir -p "$APP_DIR/data"

SERVICE_FILE=/etc/systemd/system/p1pro.service

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=P1Pro Console (fnOS) - Sunlogin P1Pro smart strip dashboard
Documentation=https://github.com/52sanmao/sunlogin-p1pro-nas
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$PY $APP_DIR/app/server/app.py -c $APP_DIR/config.json
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
# 让"今天""本月"按本地自然日切分
Environment=TZ=Asia/Shanghai

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable p1pro >/dev/null 2>&1 || true
systemctl restart p1pro

sleep 2
if systemctl is-active --quiet p1pro; then
  PORT=$(grep -o '"port"[[:space:]]*:[[:space:]]*[0-9]*' "$APP_DIR/config.json" | tail -1 | grep -o '[0-9]*$')
  echo ""
  echo "安装完成，服务已启动。"
  echo "  访问地址：http://<NAS-IP>:${PORT:-8787}"
  echo "  查看日志：journalctl -u p1pro -f"
  echo "  连通性自检：$PY $APP_DIR/app/server/app.py --check"
else
  echo "服务启动失败，用 journalctl -u p1pro -n 50 看原因" >&2
  exit 1
fi

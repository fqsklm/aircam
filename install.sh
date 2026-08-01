#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo ./install.sh 运行" >&2
  exit 1
fi

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

apt-get update
apt-get install -y acl curl ffmpeg v4l-utils python3-gpiozero python3-lgpio

if ! getent group gpio >/dev/null; then
  groupadd --system gpio
fi
if ! id aircam >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/aircam --create-home \
    --shell /usr/sbin/nologin --groups video,gpio aircam
fi

install -d -o pi -g pi -m 0755 \
  /home/pi/AirCam /home/pi/AirCam/web /home/pi/AirCam/scripts \
  /home/pi/AirCam/systemd
install -d -o pi -g aircam -m 0750 /home/pi/AirCam/config
install -d -o pi -g pi -m 0755 /home/pi/Pictures
install -d -o aircam -g pi -m 2770 /home/pi/Pictures/AirCam
setfacl -m u:aircam:--x /home/pi /home/pi/Pictures
if [[ "$project_dir" != "/home/pi/AirCam" ]]; then
  install -o pi -g pi -m 0755 "$project_dir/aircam.py" /home/pi/AirCam/aircam.py
  install -o pi -g pi -m 0644 "$project_dir/web/index.html" /home/pi/AirCam/web/index.html
  install -o pi -g pi -m 0755 \
    "$project_dir/scripts/probe-camera.sh" \
    "$project_dir/scripts/create-hotspot.sh" \
    "$project_dir/scripts/self-test.sh" \
    /home/pi/AirCam/scripts/
  install -o pi -g pi -m 0644 \
    "$project_dir/systemd/aircam.service" /home/pi/AirCam/systemd/aircam.service
fi

if [[ ! -f /home/pi/AirCam/config/config.json ]]; then
  token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  sed "s/CHANGE_ME/$token/" "$project_dir/config.example.json" \
    > /home/pi/AirCam/config/config.json
  chown pi:aircam /home/pi/AirCam/config/config.json
  chmod 0640 /home/pi/AirCam/config/config.json
  echo "已生成访问令牌：$token"
  echo "请立即保存；以后可在 /home/pi/AirCam/config/config.json 中查看或更改。"
else
  echo "保留已有配置：/home/pi/AirCam/config/config.json"
fi

install -o root -g root -m 0644 \
  "$project_dir/systemd/aircam.service" /etc/systemd/system/aircam.service
systemctl daemon-reload
systemctl enable aircam.service

echo
echo "安装完成。请先编辑 /home/pi/AirCam/config/config.json，使分辨率和控制项匹配摄像头。"
echo "验证配置：sudo -u aircam /usr/bin/python3 /home/pi/AirCam/aircam.py --config /home/pi/AirCam/config/config.json --check-config"
echo "启动服务：sudo systemctl start aircam"
echo "查看日志：journalctl -u aircam -f"

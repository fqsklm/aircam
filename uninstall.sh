#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo ./uninstall.sh 运行" >&2
  exit 1
fi

systemctl disable --now aircam.service 2>/dev/null || true
rm -f /etc/systemd/system/aircam.service
systemctl daemon-reload

echo "程序服务已移除。"
echo "为防止误删照片，以下内容仍被保留："
echo "  /home/pi2/AirCam/config"
echo "  /home/pi2/AirCam/pictures"
echo "  /var/lib/aircam（旧版运行数据，如存在）"
echo "如确认不再需要，请手工备份后删除。"

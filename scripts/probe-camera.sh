#!/usr/bin/env bash
set -euo pipefail

device="${1:-/dev/video0}"

if [[ ! -e "$device" ]]; then
  echo "找不到设备：$device" >&2
  exit 1
fi

echo "### USB 设备"
lsusb
echo
echo "### V4L2 设备"
v4l2-ctl --list-devices
echo
echo "### 格式和分辨率：$device"
v4l2-ctl -d "$device" --list-formats-ext
echo
echo "### 可控制参数：$device"
v4l2-ctl -d "$device" --list-ctrls-menus
echo
echo "### 稳定设备路径"
for path in /dev/v4l/by-id/*; do
  [[ -e "$path" ]] || continue
  if [[ "$(readlink -f "$path")" == "$(readlink -f "$device")" ]]; then
    echo "$path"
  fi
done

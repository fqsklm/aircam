#!/usr/bin/env bash
set -euo pipefail

connection_name="AirCam-Hotspot"
interface="wlan0"
ssid="${1:-AirCam}"
password="${2:-}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo $0 [SSID] [密码] 运行" >&2
  exit 1
fi
if ! command -v nmcli >/dev/null 2>&1; then
  echo "找不到 nmcli；此脚本需要 Raspberry Pi OS Bookworm 或更新版本的 NetworkManager。" >&2
  exit 1
fi
if [[ -z "$password" ]]; then
  read -r -s -p "输入热点密码（8–63 个字符）：" password
  echo
fi
if (( ${#ssid} < 1 || ${#ssid} > 32 )); then
  echo "SSID 长度必须为 1–32 个字符。" >&2
  exit 1
fi
if (( ${#password} < 8 || ${#password} > 63 )); then
  echo "热点密码长度必须为 8–63 个字符。" >&2
  exit 1
fi
if ! nmcli -t -f DEVICE,TYPE device | grep -q "^${interface}:wifi$"; then
  echo "找不到 Wi-Fi 接口 ${interface}；请先运行 nmcli device 检查接口名。" >&2
  exit 1
fi
if nmcli -t -f NAME connection show | grep -Fxq "$connection_name"; then
  echo "连接配置 $connection_name 已存在，未做修改。" >&2
  echo "如需重建，请先执行：sudo nmcli connection delete '$connection_name'" >&2
  exit 1
fi

# Create the profile without activating it, so an SSH session over wlan0 is not
# disconnected in the middle of setup. A negative priority lets known client
# networks win when available; the hotspot remains the field fallback.
nmcli connection add \
  type wifi \
  ifname "$interface" \
  con-name "$connection_name" \
  autoconnect yes \
  ssid "$ssid"
nmcli connection modify "$connection_name" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  ipv4.method shared \
  ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "$password" \
  connection.autoconnect-priority -10

echo "热点配置已创建，但尚未切换当前网络。"
echo "SSID：$ssid"
echo "启动热点（会断开 wlan0 当前连接）："
echo "  sudo nmcli connection up '$connection_name'"
echo "停用热点并恢复普通 Wi-Fi："
echo "  sudo nmcli connection down '$connection_name'"
echo "  sudo nmcli device up '$interface'"
echo "热点启动后，树莓派地址通常是 10.42.0.1；请用 nmcli device show $interface 确认。"

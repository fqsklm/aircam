#!/usr/bin/env bash
set -euo pipefail

config="${AIR_CAM_CONFIG:-/home/pi2/AirCam/config/config.json}"
capture_test=false
if [[ "${1:-}" == "--capture" ]]; then
  capture_test=true
elif [[ -n "${1:-}" ]]; then
  echo "用法：$0 [--capture]" >&2
  exit 2
fi

pass=0
warn=0
fail=0

ok() {
  pass=$((pass + 1))
  echo "[通过] $*"
}
warning() {
  warn=$((warn + 1))
  echo "[警告] $*"
}
failure() {
  fail=$((fail + 1))
  echo "[失败] $*"
}

echo "AirCam 树莓派自检"
echo "时间：$(date --iso-8601=seconds)"
echo

if [[ -r /proc/device-tree/model ]]; then
  model="$(tr -d '\0' < /proc/device-tree/model)"
  echo "硬件：$model"
  if [[ "$model" == *"Raspberry Pi 5"* ]]; then
    ok "检测到 Raspberry Pi 5"
  else
    warning "当前设备不是 Raspberry Pi 5"
  fi
else
  warning "无法读取树莓派型号"
fi

if command -v vcgencmd >/dev/null 2>&1; then
  throttled="$(vcgencmd get_throttled)"
  temperature="$(vcgencmd measure_temp)"
  echo "电源/降频：$throttled"
  echo "温度：$temperature"
  if [[ "$throttled" == "throttled=0x0" ]]; then
    ok "未检测到当前或历史欠压/降频"
  else
    failure "检测到欠压、过热或降频标志；飞行前必须解决"
  fi
else
  warning "找不到 vcgencmd，无法检查欠压和温度"
fi

if [[ ! -r "$config" ]]; then
  failure "无法读取配置：$config"
  echo
  echo "汇总：通过 $pass，警告 $warn，失败 $fail"
  exit 1
fi

readarray -t values < <(
  python3 - "$config" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
print(config["camera"]["device"])
print(config["storage"]["data_dir"])
print(config["server"]["port"])
print(config["server"].get("token", ""))
PY
)
device="${values[0]}"
data_dir="${values[1]}"
port="${values[2]}"
token="${values[3]}"

if [[ -e "$device" ]]; then
  ok "摄像头设备存在：$device"
else
  failure "摄像头设备不存在：$device"
fi

if v4l2-ctl -d "$device" --all >/dev/null 2>&1; then
  ok "V4L2 可以打开摄像头"
else
  failure "V4L2 无法打开摄像头"
fi

if [[ -d "$data_dir" && -w "$data_dir" ]]; then
  ok "照片目录可写：$data_dir"
else
  failure "照片目录不存在或不可写：$data_dir"
fi

if [[ -d "$data_dir" ]]; then
  available_kb="$(df -Pk "$data_dir" | awk 'NR==2 {print $4}')"
else
  available_kb=0
fi
if [[ "$available_kb" =~ ^[0-9]+$ ]] && (( available_kb >= 1048576 )); then
  ok "照片目录剩余空间不少于 1 GiB"
else
  warning "照片目录剩余空间不足 1 GiB"
fi

if systemctl is-enabled --quiet aircam.service; then
  ok "aircam.service 已设为开机启动"
else
  failure "aircam.service 未设为开机启动"
fi
if systemctl is-active --quiet aircam.service; then
  ok "aircam.service 正在运行"
else
  failure "aircam.service 未运行"
fi

status_url="http://127.0.0.1:${port}/api/status"
if status_json="$(curl --silent --show-error --fail \
  -H "X-AirCam-Token: $token" "$status_url")"; then
  ok "本机控制 API 响应正常"
else
  failure "本机控制 API 无法访问"
  status_json=""
fi

if [[ "$capture_test" == true && -n "$status_json" ]]; then
  current_state="$(
    python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["state"])' \
      <<<"$status_json"
  )"
  if [[ "$current_state" != "idle" && "$current_state" != "error" ]]; then
    failure "当前状态为 $current_state，为避免打断任务，未运行试拍"
  else
    echo "运行 4 秒试拍……"
    if curl --silent --show-error --fail \
      -X POST \
      -H "X-AirCam-Token: $token" \
      -H "Content-Type: application/json" \
      -d '{"interval_seconds":1}' \
      "http://127.0.0.1:${port}/api/start" >/dev/null; then
      sleep 4
      if stop_json="$(
          curl --silent --show-error --fail \
          -X POST \
          -H "X-AirCam-Token: $token" \
          -H "Content-Type: application/json" \
          -d '{}' \
          "http://127.0.0.1:${port}/api/stop"
        )"; then
        photo_count="$(
          python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["photo_count"])' \
            <<<"$stop_json"
        )"
        if (( photo_count >= 2 )); then
          ok "试拍成功，共生成 $photo_count 张照片"
        else
          failure "试拍完成但只发现 $photo_count 张照片"
        fi
      else
        failure "试拍已开始，但无法正常停止；请立即检查服务状态"
      fi
    else
      failure "无法启动试拍"
    fi
  fi
fi

echo
echo "汇总：通过 $pass，警告 $warn，失败 $fail"
if (( fail > 0 )); then
  exit 1
fi

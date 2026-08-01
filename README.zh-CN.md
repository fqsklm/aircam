# AirCam：树莓派 5 固定翼 USB 拍照系统

AirCam 面向支持 Linux UVC/V4L2 的 USB 摄像头。接通树莓派电源后，系统服务自动运行；用户可以通过网页或一个 3.3V 安全的 GPIO 电平开始、停止连续拍照，并通过 V4L2 设置曝光、增益、白平衡等摄像头实际支持的参数。

## 1. 系统边界

- 软件支持：Raspberry Pi OS Lite 64-bit、USB UVC/V4L2 摄像头、JPEG 定时连续拍照、网页控制、可选 GPIO 高/低电平控制。
- 曝光能力取决于摄像头固件。标注“免驱”只说明它通常兼容 UVC，不代表一定支持手动曝光。
- GPIO 输入只接受树莓派安全的 3.3V 逻辑或隔离后的开关信号。不得把 5V 接收机 PWM 信号直接接到 GPIO。
- 当前 GPIO 功能是电平控制：有效电平开始、无效电平停止。PWM、SBUS、CRSF 需要根据遥控接收机或飞控型号增加相应适配器。
- 网页控制适合台架和近距离调试；飞行中的主控制链路建议使用遥控接收机或飞控输出。
- 拍照进程意外退出时默认自动恢复三次。照片编号从已有文件后继续，不覆盖先前照片。
- 默认至少保留 512MB 磁盘空间；低于阈值会拒绝开始或主动停止拍照。

## 2. 推荐硬件

- Raspberry Pi 5（2GB 即可）
- 兼容 UVC、支持 MJPEG 输出的 USB 摄像头
- 高耐久 microSD 卡，建议 32GB 或更大
- 台架阶段：官方 27W USB-C 电源
- 飞行阶段：与动力电池电压匹配、连续输出不低于 5.1V/5A 的独立 BEC；必须进行压降、纹波及电机干扰测试
- 树莓派 5 Active Cooler，或经过温升测试的散热片和机身风道
- 遥控触发时使用光耦、晶体管电平转换，或飞控原生 3.3V GPIO
- 可选：树莓派 5 RTC 电池，用于无网络时保持准确日期

## 3. 安装 Raspberry Pi OS

在 Raspberry Pi Imager 中选择 Raspberry Pi OS Lite 64-bit。写卡前配置：

1. 主机名，例如 `aircam`
2. 用户名和高强度密码
3. Wi-Fi 国家、SSID、密码
4. 启用 SSH，优先使用公钥认证
5. 正确时区

先使用可靠的台架电源，不要在首次安装时连接航模动力系统。

## 4. 探测 USB 摄像头

把本目录复制到树莓派，执行：

```bash
sudo apt update
sudo apt install -y v4l-utils ffmpeg
chmod +x scripts/probe-camera.sh
./scripts/probe-camera.sh /dev/video0 | tee camera-report.txt
```

重点检查：

- `--list-formats-ext` 中是否有 MJPG/MJPEG，以及目标分辨率和帧率。
- `--list-ctrls-menus` 中是否出现曝光、白平衡、亮度等控制项。不同 UVC 摄像头使用的名称可能不同。
- 如果 `/dev/v4l/by-id/` 有对应路径，优先把它写入配置，避免多个摄像头导致 `/dev/video0` 编号变化。

本项目当前实测的 HDR CAMERA-A 使用 `auto_exposure=1` 表示手动模式、`auto_exposure=3` 表示自动模式；手动曝光时间字段为 `exposure_time_absolute`，单位是 100 微秒，例如值 100 表示约 10 毫秒。更换摄像头后仍必须以本机列出的菜单为准。

## 5. 安装 AirCam

```bash
chmod +x install.sh uninstall.sh
sudo ./install.sh
sudo nano /home/pi/AirCam/config/config.json
```

把 `device`、`input_format`、分辨率、帧率和 `controls` 改成探测报告中确实支持的值。如果某个控制项不存在，应从配置中删除。

测试配置并启动：

```bash
sudo -u aircam python3 /home/pi/AirCam/aircam.py \
  --config /home/pi/AirCam/config/config.json \
  --check-config
sudo systemctl start aircam
systemctl status aircam --no-pager
journalctl -u aircam -n 100 --no-pager
```

安装脚本已经执行 `systemctl enable aircam`，以后接通电源，树莓派启动后会自动运行服务。

## 6. 网页操作

查看树莓派地址：

```bash
hostname -I
```

在同一网络的手机或电脑打开：

```text
http://树莓派IP:8080/
```

输入安装脚本显示的访问令牌。令牌也保存在 `/home/pi/AirCam/config/config.json`。网页支持：

- 开始、结束连续拍照
- 临时修改拍摄间隔
- 设置手动曝光和增益
- 通过JSON对象设置摄像头支持的任意整数型V4L2参数，例如白平衡、对焦、亮度和对比度
- 查看摄像头支持的完整参数
- 查看当前任务的最新照片、数量和剩余磁盘空间

API 示例：

```bash
TOKEN='替换成真实令牌'
curl -H "X-AirCam-Token: $TOKEN" http://aircam.local:8080/api/status
curl -X POST -H "X-AirCam-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"interval_seconds":1}' \
  http://aircam.local:8080/api/start
curl -X POST -H "X-AirCam-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://aircam.local:8080/api/stop
curl -X POST -H "X-AirCam-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"controls":{"exposure_auto":1,"exposure_absolute":100,"gain":0}}' \
  http://aircam.local:8080/api/controls
```

## 7. 文件结构

默认照片位于：

```text
/home/pi/Pictures/AirCam/
  state.json
  20260731_153000/
    session.json
    manifest.csv
    control-events.jsonl
    ffmpeg.log
    photo_20260801T071530.123456Z_00000001.jpg
    photo_20260801T071531.123489Z_00000002.jpg
```

每次开始拍照都会建立新任务目录。结束拍照时，服务向 ffmpeg 发送正常中断信号并等待文件关闭。

`manifest.csv`为每张已完成的照片记录：

- 文件名
- 摄像头帧的拍摄时间（UTC，微秒）和 Unix 微秒整数
- 时间来源（正常为`v4l2_pts_abs`，异常退出时可能回退为`file_mtime_fallback`）
- 文件写入时间和“写入时间减拍摄时间”的延迟
- 文件大小
- 拍摄间隔
- 当次使用的曝光、增益和白平衡配置

照片名格式为`photo_YYYYMMDDTHHMMSS.ffffffZ_NNNNNNNN.jpg`。其中`Z`表示UTC，`ffffff`是六位微秒，最后八位是任务内连续序号，用于避免重名并在进程恢复后继续编号。

`control-events.jsonl`记录飞行中每次参数变更的应用时间。服务按照摄像头帧的拍摄时间选择当时最近一次生效的参数，而不是按照较晚的文件写入时间。

正常拍摄使用V4L2帧的绝对时间戳，比文件落盘时间更接近相机产生该帧的时刻；微秒是记录分辨率，不代表系统时钟天然具有微秒级绝对准确度。要与GPS、飞控姿态高精度对齐，还需要用GPS/PPS或网络授时校准树莓派系统时钟。

配置中的可靠性选项：

```json
"capture": {
  "auto_restart": true,
  "max_restarts": 3,
  "restart_delay_seconds": 2
},
"storage": {
  "min_free_mb": 512
}
```

## 8. GPIO 遥控触发

先在台架上使用普通开关或 3.3V 信号验证。配置示例：

```json
"gpio": {
  "enabled": true,
  "bcm_pin": 17,
  "active_high": true,
  "pull_up": null,
  "bounce_time": 0.15
}
```

这是 BCM 编号，不是物理针脚号。BCM17 对应 40 针排针的物理针脚 11。

推荐接线原则：

1. 接收机或飞控信号先进入光耦或电平转换电路。
2. 转换后的输出只能是 0V/3.3V。
3. 若不是光耦隔离，接收机与树莓派需要共地。
4. 增加合适的上拉或下拉，使接收机掉电、断线时默认停止拍照。
5. 螺旋桨拆除后进行所有台架测试。

普通航模接收机的舵机口输出是约 1–2 毫秒脉宽的 PWM，不是持续高低电平，不能直接使用当前模式。可选择：

- 使用接收机控制的电子开关/继电器模块，转换成持续开关量；
- 由飞控把遥控通道映射为 3.3V GPIO；
- 后续根据接收机型号实现 PWM、SBUS 或 CRSF 解码。

## 9. 外场 Wi-Fi

最简单的方式是让树莓派连接手机热点。也可以让树莓派自己建立2.4GHz热点：

```bash
chmod +x scripts/create-hotspot.sh
sudo ./scripts/create-hotspot.sh AirCam
sudo nmcli connection up AirCam-Hotspot
```

脚本会先创建配置而不立即断开当前SSH；最后一条命令才会切换`wlan0`。热点启动后树莓派通常位于`10.42.0.1`，应以`nmcli device show wlan0`结果为准。

热点仅用于地面配置和近距离备用控制。Wi-Fi覆盖范围、机体遮挡和当地法规都可能限制连接，不能把它当作固定翼飞行中的唯一控制链路。

## 10. 一键自检

安装并配置完成后运行：

```bash
chmod +x scripts/self-test.sh
sudo ./scripts/self-test.sh
sudo ./scripts/self-test.sh --capture
```

第二条命令还会执行约4秒试拍。自检会检查树莓派型号、欠压/降频、温度、摄像头、存储、开机服务和本机API。

安装完成后也可以直接运行`sudo /home/pi/AirCam/scripts/self-test.sh --capture`。

## 11. 飞行可靠性检查

至少完成以下测试后再装机：

1. 连续拍照 2 小时，照片编号连续、服务无异常退出。
2. 同时运行摄像头和散热风扇，确认没有欠压：

   ```bash
   vcgencmd get_throttled
   dmesg | grep -i voltage
   ```

3. 从怠速到全油门反复切换，监测 5V 电压、照片损坏和 USB 断连。
4. 测量 CPU 温度：

   ```bash
   vcgencmd measure_temp
   ```

5. 验证遥控失联时拍照系统进入预定状态。
6. 拍照过程中直接断电 50 次，检查系统能否全部重新启动；正式方案最好增加受控关机或后备供电。
7. 做机身振动、USB 插头锁固、散热和重心检查。

不要一开始就启用只读根文件系统：照片目录需要单独规划为可写分区或独立存储。完成存储布局后再启用 overlayfs，会更适合频繁硬断电的航空环境。

完整逐项验收标准见`ACCEPTANCE.zh-CN.md`。

## 12. 下一步需要确定

为了完成飞行版配置，需要：

- 摄像头探测报告 `camera-report.txt`
- 航模电池是几 S
- 接收机或飞控型号、可用输出类型
- 目标分辨率及拍摄间隔
- 是否需要 GPS 坐标写入照片或任务日志

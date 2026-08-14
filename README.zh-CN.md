# AirCam：树莓派 5 固定翼 USB 拍照系统

AirCam 面向支持 Linux UVC/V4L2 的 USB 摄像头。接通树莓派电源后，系统服务自动运行；用户可以通过网页、3.3V 安全的 GPIO 电平、飞控舵机 PWM 或备用 UART/MAVLink 遥控通道开始、停止连续拍照，并通过 V4L2 设置曝光、增益、白平衡等摄像头实际支持的参数。

## 1. 系统边界

- 软件支持：Raspberry Pi OS Lite 64-bit、USB UVC/V4L2 摄像头、JPEG 定时连续拍照、网页控制、可选 GPIO 电平或 UART/MAVLink 遥控通道控制。
- 曝光能力取决于摄像头固件。标注“免驱”只说明它通常兼容 UVC，不代表一定支持手动曝光。
- GPIO 输入只接受树莓派安全的 3.3V 逻辑或隔离后的开关信号。不得把 5V 接收机 PWM 信号直接接到 GPIO。
- MAVLink 模式从飞控的 `RC_CHANNELS` 消息读取一个遥控通道，具有阈值迟滞、去抖、串口重连和通信超时停止保护。
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
- 遥控触发可使用飞控原生 3.3V UART/MAVLink；电平触发模式则使用光耦、电平转换或飞控原生 3.3V GPIO
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

## 6. 配置参数总表

持久配置文件位于：

```text
/home/pi/AirCam/config/config.json
```

“持久”表示服务重启或树莓派重新上电后仍然生效。`config.example.json` 是完整模板；实际运行时修改 `config/config.json`，不要只改示例文件。

### 6.1 摄像头参数 `camera`

| 参数 | 模板值 | 作用与取值 |
| --- | --- | --- |
| `device` | `/dev/video0` | 摄像头设备路径。多摄像头环境优先使用稳定的 `/dev/v4l/by-id/...` 路径。 |
| `input_format` | `mjpeg` | 摄像头输入格式，必须是摄像头实际支持的格式，例如 `mjpeg`。 |
| `controls` | JSON 对象 | 服务启动时应用的 V4L2 参数。参数名和值必须由当前摄像头支持；值只能是整数或布尔值。拍摄进程自动恢复时会重新应用。 |

先读取当前摄像头真正支持的控制项：

```bash
v4l2-ctl -d /dev/video0 --list-ctrls-menus
```

本项目当前实测摄像头的常用控制项如下。范围和含义仍以该命令在本机的输出为准：

| 参数 | 作用 |
| --- | --- |
| `auto_exposure` | `1` 为手动曝光，`3` 为自动曝光。 |
| `exposure_time_absolute` | 手动曝光时间，单位为 0.1 毫秒；例如 `100` 约为 10 毫秒。数值越小越有利于抑制高速运动模糊，但画面会更暗。 |
| `white_balance_automatic` | `1` 自动白平衡，`0` 手动白平衡。 |
| `white_balance_temperature` | 手动白平衡色温；通常要先关闭自动白平衡。 |
| `power_line_frequency` | 电源频率防闪烁模式，具体菜单值由摄像头决定。 |
| `backlight_compensation` | 逆光补偿。 |
| `brightness`、`contrast`、`gain` 等 | 仅在摄像头列出并支持时才能设置。 |

### 6.2 拍摄参数 `capture`

| 参数 | 模板值 | 允许范围 | 作用 |
| --- | ---: | ---: | --- |
| `width` | `2592` | 正整数 | 输出照片宽度，必须与摄像头所支持的分辨率匹配。 |
| `height` | `1944` | 正整数 | 输出照片高度，必须与摄像头所支持的分辨率匹配。 |
| `source_fps` | `30` | `1`–`240` | 摄像头输入帧率。不能高于该分辨率下摄像头实际支持的帧率。 |
| `interval_seconds` | `1.0` | `0.0334`–`3600` 秒 | 默认拍摄间隔。`0.0334` 秒约为每秒 30 张；实际速度还受摄像头帧率、曝光和存储速度限制。 |
| `jpeg_quality` | `2` | `2`–`31` | FFmpeg JPEG 质量值；数值越小质量越高、文件通常越大。 |
| `auto_restart` | `true` | `true`/`false` | 拍摄进程意外退出后是否自动恢复。 |
| `max_restarts` | `3` | `0`–`100` | 单次拍摄任务最多自动恢复多少次；`0` 表示不尝试恢复。网页上的“恢复次数”就是本次任务已经执行的次数。 |
| `restart_delay_seconds` | `2` | `0.1`–`300` 秒 | 每次自动恢复前等待的时间。短暂 USB 故障可在等待后恢复；等待期间可能漏拍，系统不会补拍。 |

开始新的拍摄任务时，“恢复次数”会重新从 `0` 计数。恢复可能由 USB 接触或供电不稳、摄像头无响应、FFmpeg 异常退出等情况触发。

### 6.3 存储参数 `storage`

| 参数 | 模板值 | 允许范围 | 作用 |
| --- | --- | --- | --- |
| `data_dir` | `/home/pi/AirCam/pictures` | 有写权限的非空路径 | 照片、任务清单和状态文件的保存根目录。修改后不会自动搬迁旧照片。 |
| `min_free_mb` | `512` | `16`–`1048576` MB | 磁盘最少保留空间。低于该值时拒绝开始拍摄；拍摄中低于该值时自动停止，防止磁盘被写满。 |

### 6.4 网页服务参数 `server`

| 参数 | 模板值 | 作用与取值 |
| --- | --- | --- |
| `host` | `0.0.0.0` | 监听地址。`0.0.0.0` 表示允许通过树莓派的各网络接口访问。 |
| `port` | `8080` | 网页端口，允许 `1`–`65535`。修改后访问地址也要改，例如 `http://树莓派IP:新端口/`。 |
| `token` | 安装时生成 | 网页和 API 的访问令牌。修改后，浏览器中也要保存新令牌。不要把真实令牌提交到公开仓库。 |

### 6.5 遥控触发参数 `gpio`

| 参数 | 模板值 | 作用与取值 |
| --- | --- | --- |
| `enabled` | `false` | 是否启用 GPIO 电平触发。启用前必须完成 3.3V 安全接线测试。 |
| `bcm_pin` | `17` | BCM GPIO 编号，不是排针物理编号；BCM17 对应物理针脚 11。 |
| `active_high` | `true` | `true` 表示高电平开始拍摄、低电平停止；`false` 表示逻辑相反。 |
| `pull_up` | `null` | `true` 使用内部上拉，`false` 使用内部下拉，`null` 不启用内部上下拉。应与外部电路和失联安全状态匹配。 |
| `bounce_time` | `0.15` | 输入去抖时间，单位秒，用于避免机械开关抖动造成重复触发。 |

GPIO 输入只允许树莓派安全的 0/3.3V 数字电平。5V、接收机舵机 PWM、SBUS 或 CRSF 都不能直接接入。

### 6.6 飞控 PWM 触发参数 `pwm`

| 参数 | 模板值 | 作用与取值 |
| --- | --- | --- |
| `enabled` | `false` | 是否启用飞控舵机 PWM 脉宽触发。与 `gpio.enabled`、`mavlink.enabled` 最多只能启用一个。 |
| `bcm_pin` | `17` | 树莓派 BCM GPIO 编号；BCM17 对应物理针脚 11。 |
| `gpiochip` | `4` | Linux GPIO 芯片编号；当前 Raspberry Pi 5 的 40 针排针由 `gpiochip4 [pinctrl-rp1]` 管理。程序使用 libgpiod 内核边沿事件时间戳测量脉宽。升级系统或更换树莓派型号后，应先用 `gpiodetect` 核对。 |
| `start_pwm` | `1700` | 合法脉宽达到或超过该值并稳定后开始拍照。 |
| `stop_pwm` | `1300` | 合法脉宽达到或低于该值并稳定后停止拍照。 |
| `min_valid_pwm` | `750` | 小于该值的脉冲视为干扰，不参与控制。 |
| `max_valid_pwm` | `2250` | 大于该值的脉冲视为干扰，不参与控制。 |
| `debounce_seconds` | `0.15` | 新拨杆位置至少保持的时间，允许 `0`–`5` 秒。 |
| `min_stable_pulses` | `5` | 执行动作前至少连续收到的同方向合法脉冲数，允许 `2`–`50`。 |
| `timeout_seconds` | `0.5` | 收不到合法 PWM 多久后判定信号中断，允许 `0.1`–`10` 秒。 |
| `stop_on_timeout` | `true` | PWM 中断时是否安全停止当前拍摄；飞行使用应保持 `true`。 |

PWM 输入使用 libgpiod 提供的内核边沿事件时间戳测量约 `1000–2000 µs` 的舵机脉宽，避免 Python 回调调度延迟直接影响测量结果，也不会把 PWM 误当作持续高低电平。网页会显示当前脉宽、频率、有效/异常脉冲数和拨杆判定。只有收到过合法 PWM 后，超时保护才会动作。

### 6.7 飞控串口触发参数 `mavlink`（保留但当前不使用）

| 参数 | 模板值 | 作用与取值 |
| --- | --- | --- |
| `enabled` | `false` | 是否启用 UART/MAVLink 遥控通道触发。与 `gpio.enabled` 不要同时启用。 |
| `device` | `/dev/serial0` | 树莓派串口设备；GPIO14/15 的主串口通常使用该稳定别名。 |
| `baud` | `115200` | 串口波特率，必须与飞控对应的 `SERIALx_BAUD` 一致。 |
| `rc_channel` | `9` | 用于控制拍摄的遥控通道，允许 RC1–RC18；不得占用飞行模式或关键操纵通道。 |
| `start_pwm` | `1700` | 通道值达到或超过该值并稳定后开始连续拍照。 |
| `stop_pwm` | `1300` | 通道值达到或低于该值并稳定后停止拍照；必须小于 `start_pwm`。 |
| `debounce_seconds` | `0.15` | 开关状态必须连续稳定的时间，允许 `0`–`5` 秒。 |
| `timeout_seconds` | `2.0` | 收不到有效 `RC_CHANNELS` 消息多久后判定通信超时，允许 `0.5`–`60` 秒。 |
| `stop_on_timeout` | `true` | 超时或串口故障时是否安全停止当前拍摄任务；飞行使用应保持 `true`。 |
| `reconnect_seconds` | `2.0` | 串口异常后重新打开设备的等待时间，允许 `0.1`–`60` 秒。 |
| `request_rate_hz` | `10` | 收到飞控心跳后，AirCam 主动请求 `RC_CHANNELS` 的频率，允许 `1`–`50` Hz。 |

AirCam 校验 MAVLink 1/2 帧和 `RC_CHANNELS` 校验和。开关位于中间区间时保持原状态，避免临界值抖动导致反复启停。只有收到过有效遥控通道后，通信超时保护才会动作。

### 6.8 网页临时设置与持久设置的区别

- 网页选择的“拍摄间隔”只用于下一次开始的拍摄任务，不会改写 `config.json`。
- 网页选择的“自动停止时长”只用于下一次任务；`0` 表示不限时，最长可设置 7 天。倒计时在树莓派本地使用单调时钟运行，Wi-Fi 断开或系统时间校准不会中断或改变时长。
- 网页应用的曝光、白平衡等控制项会立即生效，但不会改写 `config.json`；服务重启后恢复为 `camera.controls` 中的值。
- 网页曝光模式、范围、步长和当前值来自摄像头硬件读回；批量设置任一项失败或读回不一致时，服务会尝试恢复设置前的硬件值并返回错误，不会把网页默认值冒充为当前值。
- 要让某项参数在重新上电后仍然生效，应把它写入 `config/config.json`。
- 修改分辨率、帧率、存储路径、服务端口、令牌、自动恢复、GPIO、PWM 或 MAVLink 参数后，需要检查配置并重启服务。

安全修改流程：

```bash
sudo nano /home/pi/AirCam/config/config.json
sudo -u aircam python3 /home/pi/AirCam/aircam.py \
  --config /home/pi/AirCam/config/config.json \
  --check-config
sudo systemctl restart aircam
systemctl status aircam --no-pager
```

如果 `--check-config` 报错，不要重启服务；先修正 JSON、参数类型或数值范围。正在拍摄时应先在网页点击“结束拍照”，再修改配置和重启服务。

## 7. 网页操作

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
- 设置本次任务的自动停止时长并查看剩余时间
- 设置手动曝光和增益
- 通过JSON对象设置摄像头支持的任意整数型V4L2参数，例如白平衡、对焦、亮度和对比度
- 查看摄像头支持的完整参数
- 查看当前任务的最新照片、数量和剩余磁盘空间
- 按任务查看照片数量和占用空间
- 将完整任务以流式 ZIP 导入电脑，并删除单个任务或清空全部照片

API 示例：

```bash
TOKEN='替换成真实令牌'
curl -H "X-AirCam-Token: $TOKEN" http://aircam.local:8080/api/status
curl -X POST -H "X-AirCam-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"interval_seconds":1,"duration_seconds":600}' \
  http://aircam.local:8080/api/start
curl -X POST -H "X-AirCam-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}' \
  http://aircam.local:8080/api/stop
curl -X POST -H "X-AirCam-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"controls":{"auto_exposure":1,"exposure_time_absolute":100,"gain":0}}' \
  http://aircam.local:8080/api/controls
```

API 中的 `duration_seconds` 单位为秒；省略、传入 `null` 或传入 `0` 都表示不限时。到达设定时长后，树莓派会在本地向拍摄进程发送正常停止信号，等待照片文件关闭，并把任务状态改回待机。该过程不依赖网页保持打开或网络连接。

### 7.1 照片导入和清理

网页“照片导入与清理”区域会列出每个拍摄任务的编号、开始时间、照片数量和占用空间。

- “导入电脑”先通过已认证的API申请一个60秒有效、只能使用一次的下载链接。网页完整接收ZIP并显示进度后，再以`AirCam_任务编号.zip`交给浏览器保存，从而兼容不能正确结束未知长度网络下载的浏览器。
- ZIP采用HTTP/1.1分块流式、仅存储模式发送。JPEG不会被重复压缩，树莓派也不会在照片分区生成第二份巨大压缩包。下载完成前不要关闭网页、断开网络或给树莓派断电。
- 网页兼容导入限制为512 MB，因为接收过程中会占用电脑浏览器内存。更大的正式飞行任务应使用SFTP等支持断点续传的工具；浏览器下载被中断时，重新点击“导入电脑”即可从头下载，树莓派原照片不会被修改。
- “删除任务”永久删除选中任务。“清空全部照片”要求输入同名确认文字，删除全部任务目录，但保留`/home/pi/AirCam/pictures`总目录和服务状态文件。
- 拍摄或恢复过程中禁止下载和清理；下载进行中也禁止开始新的拍摄，避免大量读取影响照片写入。
- 导入电脑不会自动删除树莓派照片。确认电脑中的ZIP能够打开、照片数量正确后，再单独执行删除。

相关API：

- `GET /api/sessions`：列出任务。
- `POST /api/sessions/<任务编号>/download-ticket`：申请一次性下载链接。
- `POST /api/sessions/<任务编号>/delete`：删除单个任务，请求体中的`confirm_session`必须与任务编号完全一致。
- `POST /api/sessions/clear`：清空全部任务，请求体必须为`{"confirm_text":"清空全部照片"}`。

## 8. 文件结构

默认照片位于：

```text
/home/pi/AirCam/pictures/
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

## 9. AET-H743 PWM 遥控触发

本方案选择飞控 PWM9/M9 输出，把遥控器 RC9 原始通道转换成标准舵机 PWM。AET-H743 的 PWM7、8、9、10 属于同一输出组；如果该组已有 DShot 电机输出，不要把 PWM9 单独改成普通 PWM，应改选不冲突的输出并相应修改 `SERVOn_FUNCTION`。

先在 Mission Planner 的遥控器校准页面确认拨杆控制 RC9：

```text
停止位置：约 1000 µs
开始位置：约 2000 µs
```

然后在 Mission Planner 的“配置/调试 → 全部参数树”设置。若使用 QGroundControl，则进入“载具设置 → 参数”，逐个搜索相同的参数名并保存：

```text
RC9_OPTION       = 0      # 该通道不绑定其他飞控辅助功能
SERVO9_FUNCTION  = 59     # RCPassThru9：RC9直通PWM9
SERVO9_MIN       = 1000
SERVO9_TRIM      = 1500
SERVO9_MAX       = 2000
SERVO9_REVERSED  = 0
```

其中真正建立 RC9 → PWM9 映射的是 `SERVO9_FUNCTION=59`。ArduPilot 的 `RCPassThru9` 会原样输出 RC9 输入脉宽，`SERVO9_MIN/TRIM/MAX` 不会把它重新缩放；因此必须先在遥控器校准页面确认 RC9 的实际低位不高于 1300 µs、高位不低于 1700 µs。这里列出的 `SERVO9_MIN/TRIM/MAX` 只是让输出通道保留常见的舵机基准值。

写入参数并重启飞控。输出受到飞控安全状态控制时，应按正常台架流程解除安全锁；不要为了测试随意禁用整机安全保护。用示波器或逻辑分析仪确认 PWM9 信号高电平不超过 3.3V、低位脉宽约 1000 µs、高位约 2000 µs，然后断电接线：

```text
AET-H743 PWM9/M9 信号 S ── 1kΩ ── 树莓派物理11脚 / BCM17
                                       │
                                      10kΩ
                                       │
AET-H743 GND ──────────────────────────┴── 树莓派GND（如物理9脚）

PWM9/M9 的“+”电源脚：不连接树莓派
```

如果实测信号高电平超过 3.3V，必须先使用电平转换、光耦或正确计算的分压电路，不能直接接树莓派。飞控舵机电源轨是 5V/6V/7V 并不代表信号脚一定同电压，仍必须实测信号脚。

AirCam 实际配置使用：

```json
"gpio": {"enabled": false},
"pwm": {
  "enabled": true,
  "bcm_pin": 17,
  "gpiochip": 4,
  "start_pwm": 1700,
  "stop_pwm": 1300,
  "min_valid_pwm": 750,
  "max_valid_pwm": 2250,
  "debounce_seconds": 0.15,
  "min_stable_pulses": 5,
  "timeout_seconds": 0.5,
  "stop_on_timeout": true
},
"mavlink": {"enabled": false}
```

遥控失联时，接收机/ArduPilot 还应把 RC9 和 PWM9 置于低位，不能“保持最后值”。AirCam 的第二层保护会在 0.5 秒收不到合法 PWM 后停止拍照。正式飞行前必须在拆桨状态验证低位停止、高位开始、遥控失联、飞控重启、拔掉信号线及动力系统干扰场景。

## 10. GPIO 遥控触发（可选旧方案）

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

普通航模接收机的舵机口输出是约 1–2 毫秒脉宽的 PWM，不是持续高低电平，不能使用本节的数字电平模式。可选择：

- 使用接收机控制的电子开关/继电器模块，转换成持续开关量；
- 由飞控把遥控通道映射为 3.3V GPIO；
- 使用上一节已经实现的 `pwm` 脉宽模式；
- 后续根据接收机型号实现 SBUS 或 CRSF 解码。

## 11. 外场 Wi-Fi

最简单的方式是让树莓派连接手机热点。也可以让树莓派自己建立2.4GHz热点：

```bash
chmod +x scripts/create-hotspot.sh
sudo ./scripts/create-hotspot.sh AirCam
sudo nmcli connection up AirCam-Hotspot
```

脚本会先创建配置而不立即断开当前SSH；最后一条命令才会切换`wlan0`。热点启动后树莓派通常位于`10.42.0.1`，应以`nmcli device show wlan0`结果为准。

热点仅用于地面配置和近距离备用控制。Wi-Fi覆盖范围、机体遮挡和当地法规都可能限制连接，不能把它当作固定翼飞行中的唯一控制链路。

## 12. 一键自检

安装并配置完成后运行：

```bash
chmod +x scripts/self-test.sh
sudo ./scripts/self-test.sh
sudo ./scripts/self-test.sh --capture
```

第二条命令还会执行约4秒试拍。自检会检查树莓派型号、欠压/降频、温度、摄像头、存储、开机服务和本机API。

安装完成后也可以直接运行`sudo /home/pi/AirCam/scripts/self-test.sh --capture`。

## 13. 飞行可靠性检查

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

## 14. 下一步需要确定

为了完成飞行版配置，需要：

- 摄像头探测报告 `camera-report.txt`
- 航模电池是几 S
- 接收机或飞控型号、可用输出类型
- 目标分辨率及拍摄间隔
- 是否需要 GPS 坐标写入照片或任务日志

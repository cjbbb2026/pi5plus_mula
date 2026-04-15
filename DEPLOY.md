# OrangePic 部署文档

本文档用于在 OrangePi RK3588 类板卡上部署当前项目。项目主要包含三部分：

- HDMI RX 画面采集和 RKNN 推理：`app/ai.py`
- 键鼠 HID 透传：`app/passthrough.py`、`scripts/km_passthrough.sh`
- Web 控制台：`app/web_console.py`，默认监听 `0.0.0.0:8080`

## 1. 部署前准备

### 1.1 硬件和系统

建议环境：

- OrangePi RK3588 系列板卡，系统为 Debian/Ubuntu/Armbian 类 Linux。
- 系统可使用 `apt-get` 安装软件包。
- 当前登录用户具备 `sudo` 权限，默认用户通常是 `orangepi`。
- HDMI RX 输入源已连接，USB OTG 口用于键鼠 HID 透传。
- 项目目录中保留以下关键文件：
  - `yolo261n-rk3588.rknn`
  - `requirements.txt`
  - `app/`
  - `config/`
  - `scripts/`

### 1.2 推荐部署目录

示例使用 `/home/orangepi/orangepic` 作为部署目录，实际路径可以不同。

```bash
cd /home/orangepi/orangepic
```

如果项目是从 Windows 或其他机器拷贝到板卡，建议确认脚本有执行权限：

```bash
chmod +x scripts/*.sh
```

## 2. 一键部署

全新系统优先使用项目内置部署脚本：

```bash
cd /home/orangepi/orangepic
sudo APP_USER=orangepi APP_HOME=/home/orangepi bash scripts/deploy.sh install
```

`scripts/deploy.sh` 会转发到 `scripts/deploy_new_system.sh install`，默认执行以下动作：

- 安装系统依赖：Python、OpenCV、evdev、v4l-utils、usbutils 等。
- 安装可选 GStreamer 包。
- 如存在 `scripts/linux-image-current-rockchip-rk3588_1.2.2_arm64.deb` 和 `scripts/linux-dtb-current-rockchip-rk3588_1.2.2_arm64.deb`，按 `INSTALL_LOCAL_KERNEL_DEBS=auto` 自动安装本地内核包。
- 创建 `.venv` 虚拟环境，并安装 `requirements.txt`。
- 如存在 `scripts/librknnrt.so` 和 `scripts/rknn_server`，安装板端 RKNN runtime 到系统目录。
- 安装或校验 `rknn-toolkit-lite2` Python 包。
- 启用 HDMI RX overlay。
- 安装开机自启服务 `orangepic-start-all.service`。
- 执行部署校验。

安装过程中如果启用了 HDMI RX overlay 或安装了内核包，建议部署完成后重启：

```bash
sudo reboot
```

重启后进入项目目录执行校验：

```bash
cd /home/orangepi/orangepic
sudo bash scripts/deploy_new_system.sh verify
```

## 3. 常用部署参数

部署脚本支持通过环境变量调整行为：

```bash
sudo APP_USER=orangepi APP_HOME=/home/orangepi ENABLE_AUTOSTART=1 START_AFTER_INSTALL=0 INSTALL_AI=1 bash scripts/deploy_new_system.sh install
```

常用变量说明：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_USER` | 当前 sudo 原始用户或 `orangepi` | 运行 AI 和 Web 进程的普通用户 |
| `APP_HOME` | `APP_USER` 的 home | 普通用户 home 目录 |
| `ENABLE_AUTOSTART` | `1` | 是否安装 systemd 开机自启 |
| `START_AFTER_INSTALL` | `0` | 部署后是否立即启动整套服务 |
| `INSTALL_AI` | `1` | 是否安装和校验 RKNN AI 运行环境 |
| `INSTALL_LOCAL_KERNEL_DEBS` | `auto` | 是否安装 `scripts/` 下的本地 RK3588 内核 deb 包，可取 `auto`、`1`、`0` |
| `RKNN_WHEEL` | 空 | 指定本地 `rknn_toolkit_lite2-*.whl` 路径 |
| `RKNN_PIP_PACKAGE` | `rknn-toolkit-lite2` | 从 pip 安装的 RKNN Lite 包名 |
| `RKNN_PIP_VERSION` | `2.3.2` | 从 pip 安装的 RKNN Lite 版本 |

只部署键鼠透传，不部署 AI：

```bash
sudo INSTALL_AI=0 ENABLE_AUTOSTART=0 bash scripts/deploy_new_system.sh install
```

离线或 pip 无法访问时，将 RKNN Lite wheel 放到项目目录，或显式指定准确 wheel 路径：

```bash
sudo RKNN_WHEEL=/home/orangepi/rknn_toolkit_lite2-2.3.2-cp310-cp310-linux_aarch64.whl bash scripts/deploy_new_system.sh install
```

如果使用 `INSTALL_AI=0` 部署，后续校验也要带同样参数：

```bash
sudo INSTALL_AI=0 bash scripts/deploy_new_system.sh verify
```

## 4. 启动、停止和状态

启动整套服务：

```bash
cd /home/orangepi/orangepic
sudo bash scripts/start_all.sh start
```

停止：

```bash
sudo bash scripts/start_all.sh stop
```

重启：

```bash
sudo bash scripts/start_all.sh restart
```

查看状态：

```bash
sudo bash scripts/start_all.sh status
```

启动成功后可访问 Web 控制台：

```text
http://<板卡IP>:8080
```

日志文件位于项目根目录：

- `.ai.log`
- `.web.log`
- `.passthrough.log`

PID 和运行状态文件：

- `.ai.pid`
- `.web.pid`
- `.ai_state.json`
- `.ai_command.json`

## 5. 开机自启

默认部署会安装 systemd 服务：

```text
orangepic-start-all.service
```

查看服务状态：

```bash
systemctl status orangepic-start-all.service --no-pager
```

手动启动：

```bash
sudo systemctl start orangepic-start-all.service
```

手动重启：

```bash
sudo systemctl restart orangepic-start-all.service
```

禁用并移除自启：

```bash
sudo bash scripts/install_start_all_autostart.sh uninstall
```

重新安装自启：

```bash
sudo APP_USER=orangepi APP_HOME=/home/orangepi bash scripts/install_start_all_autostart.sh install
```

## 6. 运行参数配置

`scripts/start_all.sh` 支持通过环境变量覆盖默认启动参数。

示例：修改 Web 端口和 AI 采集设备：

```bash
sudo WEB_PORT=8090 AI_DEVICE=/dev/video0 bash scripts/start_all.sh restart
```

常用变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEB_ENABLE` | `1` | 是否启动 Web 控制台 |
| `WEB_HOST` | `0.0.0.0` | Web 监听地址 |
| `WEB_PORT` | `8080` | Web 监听端口 |
| `AI_MODEL` | 自动选择，回退到 `yolo261n-rk3588.rknn` | RKNN 模型路径 |
| `MODEL_DIR` | `models` | Web 上传模型保存目录 |
| `CONFIG_DIR` | `config` | 模型运行配置目录 |
| `AI_DEVICE` | `auto` | HDMI RX V4L2 设备，例如 `/dev/video0` |
| `AI_BACKEND` | `v4l2-raw` | 采集后端，可取 `auto`、`gstreamer`、`v4l2`、`v4l2-raw` |
| `AI_CROP_WIDTH` | `500` | 中心裁剪宽度 |
| `AI_CROP_HEIGHT` | `500` | 中心裁剪高度 |
| `AI_PROCESS_WIDTH` | `0` | 推理前缩放宽度，`0` 表示跟随模型输入 |
| `AI_PROCESS_HEIGHT` | `0` | 推理前缩放高度，`0` 表示跟随模型输入 |
| `AI_SHOW` | `0` | 是否显示 OpenCV 预览窗口 |
| `AI_EXTRA_ARGS` | `--print-every 2` | 传给 `app/ai.py` 的额外参数 |
| `APPLY_PERF_MODE` | `1` | 启动时是否切换性能模式 |
| `APPLY_HDMIRX_EDID` | `1` | 启动时是否应用 HDMI RX EDID |
| `HDMIRX_EDID_ACTION` | 空 | 指定 EDID 预设 |
| `HDMIRX_EDID_FALLBACK_ACTION` | `standard-dual` | 未指定 EDID 时的默认预设 |
| `REQUIRE_HDMIRX_READY` | `1` | 启动 AI 前是否等待 HDMI RX ready |

配置优先级：

- 命令行环境变量优先。
- Web 控制台选择的模型和配置会写入 `.ai_models.json`、`.ai_last_model.txt` 和 `config/*.json`。
- 如果没有选择模型，默认使用项目根目录的 `yolo261n-rk3588.rknn`。

## 7. HDMI RX 检查和 EDID

查看 HDMI RX 状态：

```bash
bash scripts/hdmirx_ready.sh status
```

等待 HDMI RX 输入 ready：

```bash
bash scripts/hdmirx_ready.sh wait-ready
```

查看 HDMI RX overlay 配置：

```bash
sudo bash scripts/enable_hdmirx_overlay.sh status
```

启用 HDMI RX overlay：

```bash
sudo bash scripts/enable_hdmirx_overlay.sh enable
sudo reboot
```

查看当前 EDID：

```bash
sudo bash scripts/hdmirx_edid.sh status
```

应用默认推荐 EDID：

```bash
sudo bash scripts/hdmirx_edid.sh standard-dual
```

其他可用 EDID 预设：

- `single-1080p60`
- `single-1080p60-compat`
- `single-1080p90`
- `single-1080p120`
- `single-1080p120-compat`
- `single-1440p60`
- `builtin-1080p`
- `builtin-2k`

修改 EDID 后，通常需要重新插拔 HDMI 输入源，或让输入源重新识别显示设备。

## 8. 键鼠 HID 透传

查看透传状态：

```bash
sudo bash scripts/km_passthrough.sh status
```

列出输入设备：

```bash
sudo bash scripts/km_passthrough.sh list
```

自动选择鼠标并启动透传：

```bash
sudo bash scripts/km_passthrough.sh start-auto-mouse
```

停止透传：

```bash
sudo bash scripts/km_passthrough.sh stop
```

如果需要伪装 USB 设备信息，可以在启动前设置：

```bash
sudo USB_VID=0x046d USB_PID=0xc077 USB_MANUFACTURER='Logitech' USB_PRODUCT='USB Optical Mouse' bash scripts/km_passthrough.sh restart
```

## 9. RKNN runtime 检查

检查当前模型是否能被 RKNN runtime 正常加载：

```bash
bash scripts/check_rknn_runtime.sh yolo261n-rk3588.rknn
```

查看或安装板端 RKNN runtime 文件：

```bash
sudo bash scripts/install_rknn_runtime.sh status
sudo bash scripts/install_rknn_runtime.sh install
```

如果出现 `Invalid RKNN model version`，通常是板端 runtime/driver 版本过旧，或模型由更新 toolkit 导出。处理方式：

- 使用项目内置 `scripts/librknnrt.so` 重新安装板端 runtime。
- 更新系统 RKNN 驱动/runtime。
- 用与板端 runtime 兼容的 toolkit 重新导出 `.rknn` 模型。

## 10. 常规更新部署

如果只是更新代码、模型或配置，不需要完整重装系统依赖：

```bash
cd /home/orangepi/orangepic
sudo bash scripts/start_all.sh stop
```

替换项目文件后执行：

```bash
sudo bash scripts/deploy_new_system.sh verify
sudo bash scripts/start_all.sh start
```

如果更新了 Python 依赖、RKNN runtime、内核包或 HDMI RX overlay，再执行完整安装：

```bash
sudo bash scripts/deploy_new_system.sh install
sudo reboot
```

## 11. 常见故障排查

### 11.1 Web 控制台打不开

检查进程和日志：

```bash
sudo bash scripts/start_all.sh status
tail -n 80 .web.log
```

确认端口：

```bash
ss -lntp | grep 8080
```

如果端口冲突，换端口启动：

```bash
sudo WEB_PORT=8090 bash scripts/start_all.sh restart
```

### 11.2 HDMI RX 设备找不到

检查 HDMI RX 状态：

```bash
bash scripts/hdmirx_ready.sh status
```

如果提示 overlay 未启用：

```bash
sudo bash scripts/enable_hdmirx_overlay.sh enable
sudo reboot
```

如果 overlay 已启用但仍没有信号，检查 HDMI 输入源、线材、分辨率和刷新率，并尝试重新应用 EDID：

```bash
sudo bash scripts/hdmirx_edid.sh standard-dual
```

### 11.3 AI 启动失败

查看 AI 日志：

```bash
tail -n 120 .ai.log
```

常见原因：

- `rknnlite is not installed`：重新执行 `sudo bash scripts/deploy_new_system.sh install`，或指定 `RKNN_WHEEL`。
- `model not found`：确认 `AI_MODEL` 路径存在，或项目根目录有 `yolo261n-rk3588.rknn`。
- `Invalid RKNN model version`：参考“RKNN runtime 检查”章节。
- HDMI RX 未 ready：先执行 `bash scripts/hdmirx_ready.sh status`。

### 11.4 键鼠透传失败

检查状态：

```bash
sudo bash scripts/km_passthrough.sh status
```

检查是否能切到 USB device mode：

```bash
sudo bash scripts/km_passthrough.sh setup
```

如果找不到输入设备：

```bash
sudo bash scripts/km_passthrough.sh list
```

确认键鼠插在板卡可识别的 USB 口，OTG 口连接到目标主机。

### 11.5 systemd 自启失败

查看服务日志：

```bash
sudo journalctl -u orangepic-start-all.service -n 120 --no-pager
```

查看服务文件：

```bash
sudo bash scripts/install_start_all_autostart.sh status
```

常见原因：

- `APP_USER` 或 `APP_HOME` 设置错误。
- 项目目录移动后，旧 systemd 服务仍指向原路径。
- HDMI RX 启动时无信号，导致 `REQUIRE_HDMIRX_READY=1` 等待失败。

项目目录移动后，重新安装自启服务：

```bash
sudo bash scripts/install_start_all_autostart.sh uninstall
sudo APP_USER=orangepi APP_HOME=/home/orangepi bash scripts/install_start_all_autostart.sh install
```

## 12. 卸载

停止运行：

```bash
sudo bash scripts/start_all.sh stop
```

移除开机自启：

```bash
sudo bash scripts/install_start_all_autostart.sh uninstall
```

如需删除项目虚拟环境和运行文件：

```bash
rm -rf .venv generated models .ai.log .web.log .passthrough.log .ai.pid .web.pid .ai_state.json .ai_command.json
```

注意：上面的删除命令会移除本项目运行产生的数据和上传模型目录。执行前确认没有需要保留的模型、配置或日志。

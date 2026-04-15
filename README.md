# pi5plus_mula
orangepi5plus硬件ai源码+自带传透+自定义edid+一键部署
git https://github.com/cjbbb2026/pi5plus_mula

目前推理端yolo2026
参考官网文档导出rknn 即可 

导出代码
from ultralytics import YOLO

# Load your YOLO model
model = YOLO("best.pt")

# Export to RKNN format for a specific Rockchip platform
model.export(format="rknn", name="rk3588",imgsz=256)




自带键鼠传透  采集卡 移动 把键盘鼠标插板子上 再通过type接口接主机即可

系统推荐  Orangepi5plus_1.2.0_ubuntu_focal_desktop/server_xfce_linux6.1.43.7z
刷好系统后  通过ssh把整个文件复制过去解压 
然后给所有sh文件权限  chmod +x scripts/*.sh
然后 运行一键部署脚本  bash scripts/deploy.sh install
其他详细的看deploy.md  
再不懂 问ai



<img width="859" height="868" alt="image" src="https://github.com/user-attachments/assets/56a4db14-f5a2-4ad0-8427-693dc29171d7" />
<img width="1689" height="872" alt="image" src="https://github.com/user-attachments/assets/aa7ea000-5b1c-4991-9888-48dd5c44d97c" />
<img width="1576" height="804" alt="image" src="https://github.com/user-attachments/assets/4ebc8dab-5c9f-4d84-a683-bbae6ae92ecb" />



# 免责声明 / Disclaimer


本项目仅供学习和研究目的使用。

### 重要声明

1. **禁止用于作弊**：严禁将本项目用于任何在线游戏、竞技游戏或其他违反服务条款的场景。
2. **教育目的**：本项目旨在展示计算机视觉和目标检测技术的实现原理。
3. **法律责任**：使用者需自行承担因使用本项目而产生的一切法律责任。
4. **无担保**：本软件按"原样"提供，不提供任何明示或暗示的担保。
5. **禁止商业用途**：未经许可，不得将本项目用于任何商业目的。

### 使用限制

- 仅可用于单机游戏或个人测试环境
- 不得用于任何多人在线游戏
- 不得用于任何违反游戏服务条款的行为
- 不得用于制作或分发作弊工具

**作者不对任何滥用行为负责，包括但不限于账号封禁、法律诉讼等后果。**



This project is for educational and research purposes only.

### Important Notice

1. **No Cheating**: Using this project in online games, competitive games, or any scenario that violates terms of service is strictly prohibited.
2. **Educational Purpose**: This project demonstrates computer vision and object detection techniques.
3. **Legal Liability**: Users assume all legal responsibility for their use of this project.
4. **No Warranty**: This software is provided "as is" without any warranties.
5. **Non-Commercial**: Commercial use is prohibited without permission.

### Usage Restrictions

- Only for single-player games or personal testing environments
- Not for use in any multiplayer online games
- Not for any activities violating game terms of service
- Not for creating or distributing cheating tools

**The author is not responsible for any misuse, including but not limited to account bans or legal consequences.**



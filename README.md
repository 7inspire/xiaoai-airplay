# 小爱音箱 AirPlay & Web 控制

让小爱智能音箱支持 **AirPlay 投屏** 和 **Web 控制面板**。通过 iPhone/Mac 的 AirPlay 或浏览器直接控制音箱播放。

<img width="375" height="667" alt="IMG_7340" src="https://github.com/user-attachments/assets/35757dfa-c28d-4d91-95a6-3d334fd60cd2" />
<img width="375" height="667" alt="IMG_7341" src="https://github.com/user-attachments/assets/47ffa240-7f4a-43ea-80af-20986c02024f" />


## 功能

- **Web 控制面板** — 浏览器选歌/上传/播放/TTS/音量控制
- **AirPlay 接收** — iPhone/Mac 直接投屏到小爱音箱（shairport-sync）
- **MiService 云控制** — 通过小米云 API 兼容所有小爱型号
- **Docker 部署** — 一键部署到 NAS

## 快速开始（NAS Docker 部署）

### 1. 上传镜像

将 `xiaoai-airplay-amd64.tar` 传到 NAS，然后导入：

```bash
docker load -i xiaoai-airplay-amd64.tar
```

### 2. 创建目录

```bash
mkdir -p /volume1/docker/xiaoai-airplay/{music,conf}
```

### 3. 创建配置

```bash
# 环境变量文件
cat > /volume1/docker/xiaoai-airplay/.env << 'EOF'
MI_USER=你的userId
MI_PASS=你的passToken
HOST_IP=NAS的局域网IP
EOF

# 配置文件（可选，环境变量优先）
cp config.yaml.example /volume1/docker/xiaoai-airplay/conf/config.yaml
```

> **获取 userId 和 passToken**：参考 [miservice](https://github.com/nickel-fang/miservice_fork) 文档

### 4. 启动

将项目中的 `docker-compose.nas.yml` 复制到 NAS 部署目录，重命名为 `docker-compose.yml`：

```bash
cd /volume1/docker/xiaoai-airplay
docker compose up -d
```

> **重要**：必须使用 `network_mode: host`（NAS 专用配置已包含），否则 AirPlay mDNS 广播无法到达局域网，iPhone 将看不到 AirPlay 设备。

### 5. 使用

- **Web 控制面板**：浏览器打开 `http://NAS-IP:8080`
- **AirPlay**：iPhone 控制中心 → AirPlay → 选择「小爱音箱-AirPlay」

## 端口说明

| 端口 | 用途 |
|------|------|
| 8080 | Web 控制面板 + HTTP 音频服务 |
| 7100 | AirPlay RTSP |

## 本地开发

```bash
pip install -r requirements.txt
python -m xiaoai_airplay serve --device-ip 192.168.x.x
```

## 技术栈

- **Python 3.11** + asyncio + aiohttp
- **shairport-sync** — AirPlay 1/2 接收
- **ffmpeg** — 音频转码
- **MiService** — 小米云 API
- **Docker** — 容器化部署

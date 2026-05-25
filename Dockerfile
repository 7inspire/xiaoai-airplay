FROM python:3.11-slim

# 系统依赖：shairport-sync (AirPlay) + ffmpeg (转码) + avahi (mDNS)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    shairport-sync \
    avahi-daemon \
    avahi-utils \
    libnss-mdns \
    dbus \
    && rm -rf /var/lib/apt/lists/*

# 配置 avahi：允许在 Docker host 网络中广播
RUN sed -i 's/rlimit-nproc=3/# rlimit-nproc=3/' /etc/avahi/avahi-daemon.conf && \
    sed -i 's/#enable-dbus=yes/enable-dbus=yes/' /etc/avahi/avahi-daemon.conf && \
    sed -i 's/use-ipv6=yes/use-ipv6=no/' /etc/avahi/avahi-daemon.conf && \
    # 确保 dbus 运行目录存在
    mkdir -p /var/run/dbus

WORKDIR /app

# Python 依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码
COPY . .

# 端口：8080=HTTP/Web, 7100=AirPlay RTSP
EXPOSE 8080 7100

# 持久化：音乐文件 + 配置
VOLUME ["/app/music", "/app/conf"]

# 启动脚本
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve", "--config", "/app/conf/config.yaml", "--music-dir", "/app/music"]

#!/bin/sh
set -e

echo "[entrypoint] 启动系统服务..."

# 1. 清理旧的 dbus pid 文件（容器重启时可能残留）
rm -f /var/run/dbus/pid /var/run/dbus/system_bus_socket

# 2. 启动 dbus（avahi 依赖）
dbus-daemon --system --nofork &
DBUS_PID=$!

# 等待 dbus socket 就绪
for i in $(seq 1 10); do
    if [ -e /var/run/dbus/system_bus_socket ]; then
        break
    fi
    sleep 0.2
done

# 3. 启动 avahi-daemon（mDNS 广播，让 AirPlay 设备可被发现）
avahi-daemon --no-chroot --no-rlimits -D 2>/dev/null
sleep 0.5

# 验证 avahi 是否启动
if avahi-daemon --check 2>/dev/null; then
    echo "[entrypoint] avahi-daemon 已启动 (mDNS 广播就绪)"
else
    echo "[entrypoint] 警告: avahi-daemon 启动失败，AirPlay 可能不可见"
fi

echo "[entrypoint] 启动主服务..."

# 4. 启动 Python 主服务
exec python -m xiaoai_airplay "$@"

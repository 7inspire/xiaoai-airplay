"""命令行入口"""

import argparse
import asyncio
import logging
import signal
import sys

from .config import Config
from .server import XiaoaiAirPlayService


def main():
    parser = argparse.ArgumentParser(
        description="小爱音箱 DLNA & AirPlay 播放服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 扫描局域网 DLNA 设备
  python -m xiaoai_airplay scan

  # 启动服务（自动发现音箱）
  python -m xiaoai_airplay serve

  # 指定音箱 IP 启动
  python -m xiaoai_airplay serve --device-ip 192.168.1.100

  # 播放指定文件
  python -m xiaoai_airplay play song.mp3 --device-ip 192.168.1.100

  # 生成默认配置文件
  python -m xiaoai_airplay config
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="命令")

    # scan
    scan_parser = subparsers.add_parser("scan", help="扫描局域网 DLNA 设备")
    scan_parser.add_argument("--timeout", type=int, default=5, help="扫描超时(秒)")

    # serve
    serve_parser = subparsers.add_parser("serve", help="启动 DLNA & AirPlay 服务")
    serve_parser.add_argument("--config", "-c", default="config.yaml", help="配置文件路径")
    serve_parser.add_argument("--device-ip", help="目标音箱 IP")
    serve_parser.add_argument("--device-name", help="目标音箱名称（模糊匹配）")
    serve_parser.add_argument("--port", type=int, default=8080, help="HTTP 服务端口")
    serve_parser.add_argument("--music-dir", default="./music", help="音乐文件目录")
    serve_parser.add_argument("--airplay-name", default="小爱音箱-AirPlay", help="AirPlay 显示名称")

    # play
    play_parser = subparsers.add_parser("play", help="播放音频文件到音箱")
    play_parser.add_argument("file", help="音频文件名")
    play_parser.add_argument("--device-ip", help="目标音箱 IP")
    play_parser.add_argument("--device-name", help="目标音箱名称")
    play_parser.add_argument("--port", type=int, default=8080, help="HTTP 服务端口")

    # config
    config_parser = subparsers.add_parser("config", help="生成默认配置文件")
    config_parser.add_argument("--output", "-o", default="config.yaml", help="输出路径")

    args = parser.parse_args()

    # 日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.command == "scan":
        asyncio.run(cmd_scan(args))
    elif args.command == "serve":
        asyncio.run(cmd_serve(args))
    elif args.command == "play":
        asyncio.run(cmd_play(args))
    elif args.command == "config":
        cmd_config(args)
    else:
        parser.print_help()


async def cmd_scan(args):
    """扫描设备"""
    from .dlna_discover import DLNADiscovery

    discovery = DLNADiscovery(timeout=args.timeout)
    devices = await discovery.scan()

    if not devices:
        print("\n未发现任何 DLNA 设备")
        return

    print(f"\n发现 {len(devices)} 个 DLNA 设备:")
    print("-" * 60)
    for i, d in enumerate(devices, 1):
        avt = "✓ 支持" if d.av_transport_supported else "✗ 不支持"
        print(f"  {i}. {d.name}")
        print(f"     IP: {d.ip} | 型号: {d.model} | 制造商: {d.manufacturer}")
        print(f"     AVTransport: {avt}")
        print()


async def cmd_serve(args):
    """启动服务"""
    config = Config.from_yaml(args.config)

    if args.device_ip:
        config.target_device_ip = args.device_ip
    if args.device_name:
        config.target_device_name = args.device_name
    if args.port:
        config.http_port = args.port
    if args.music_dir:
        config.music_dir = args.music_dir
    if args.airplay_name:
        config.airplay_name = args.airplay_name

    service = XiaoaiAirPlayService(config)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(service.stop()))

    await service.start()


async def cmd_play(args):
    """播放文件"""
    config = Config()
    if args.device_ip:
        config.target_device_ip = args.device_ip
    if args.device_name:
        config.target_device_name = args.device_name
    if args.port:
        config.http_port = args.port

    service = XiaoaiAirPlayService(config)

    await service.audio_server.start()
    await service._discover_and_connect()

    if service.controller.connected:
        await service.play_file(args.file)
        print("播放中... 按 Ctrl+C 停止")
        try:
            await service._wait_playback_done()
        except KeyboardInterrupt:
            pass
    else:
        print("未能连接到音箱")

    await service.stop()


def cmd_config(args):
    """生成配置文件"""
    config = Config()
    config.to_yaml(args.output)
    print(f"默认配置已生成: {args.output}")


if __name__ == "__main__":
    main()

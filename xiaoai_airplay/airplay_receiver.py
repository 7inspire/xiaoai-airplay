"""AirPlay 接收器 - 接收 Apple 设备的音频流并转发到小爱音箱

使用 shairport-sync 处理 AirPlay 协议（包括 FairPlay 加密），
通过 stdout pipe 获取 PCM 音频数据，再用 ffmpeg 实时转码为 MP3 流。

Docker 环境下自动使用 shairport-sync；
本地开发环境若未安装则注册 mDNS 占位（不处理音频）。
"""

import asyncio
import logging
import subprocess
import signal
import os
from typing import Optional, Callable, Awaitable

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf
import socket

logger = logging.getLogger(__name__)


class AirPlayReceiver:
    """AirPlay 音频接收器

    优先使用 shairport-sync（完整 AirPlay 1/2 支持），
    未安装时回退到 mDNS 占位模式。
    """

    def __init__(
        self,
        name: str = "小爱音箱-AirPlay",
        port: int = 7100,
        on_audio_data: Optional[Callable[[bytes], Awaitable[None]]] = None,
        on_play: Optional[Callable[[], Awaitable[None]]] = None,
        on_stop: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self.name = name
        self.port = port
        self._on_audio_data = on_audio_data
        self._on_play = on_play
        self._on_stop = on_stop
        self._shairport_proc: Optional[asyncio.subprocess.Process] = None
        self._ffmpeg_proc: Optional[asyncio.subprocess.Process] = None
        self._async_zeroconf: Optional[AsyncZeroconf] = None
        self._running = False
        self._playing = False

    async def start(self) -> bool:
        """启动 AirPlay 接收器（自动选择模式）"""
        if self._check_shairport_installed():
            return await self._start_with_shairport()
        else:
            logger.warning(
                "shairport-sync 未安装，AirPlay 功能不可用。"
                "Docker 部署时会自动包含。"
            )
            # 仅注册 mDNS 占位（iPhone 能看到但无法播放）
            await self._register_mdns()
            self._running = True
            return True

    async def start_simple(self) -> bool:
        """兼容旧接口"""
        return await self.start()

    async def stop(self):
        """停止 AirPlay 接收器"""
        self._running = False
        self._playing = False

        if self._ffmpeg_proc:
            try:
                self._ffmpeg_proc.terminate()
                await self._ffmpeg_proc.wait()
            except Exception:
                pass
            self._ffmpeg_proc = None

        if self._shairport_proc:
            try:
                self._shairport_proc.terminate()
                await self._shairport_proc.wait()
            except Exception:
                pass
            self._shairport_proc = None
            logger.info("shairport-sync 已停止")

        if self._async_zeroconf:
            await self._async_zeroconf.async_close()
            self._async_zeroconf = None

        logger.info("AirPlay 接收器已停止")

    # --- shairport-sync 模式 ---

    async def _start_with_shairport(self) -> bool:
        """使用 shairport-sync (stdout 后端) 启动完整 AirPlay 接收

        shairport-sync → stdout (S16LE PCM) → [async bridge] → ffmpeg stdin → MP3 stdout → 回调
        """
        shairport_cmd = [
            "shairport-sync",
            "-a", self.name,
            "-p", str(self.port),
            "--output", "stdout",
            "-v",
        ]

        ffmpeg_cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            "-f", "s16le", "-ar", "44100", "-ac", "2",
            "-i", "pipe:0",
            "-codec:a", "libmp3lame", "-b:a", "192k",
            "-f", "mp3", "pipe:1",
        ]

        logger.info("启动 shairport-sync: %s", " ".join(shairport_cmd))
        try:
            # shairport-sync: stdout = PCM 音频, stderr = 日志
            self._shairport_proc = await asyncio.create_subprocess_exec(
                *shairport_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # ffmpeg: stdin = PCM (手动桥接), stdout = MP3
            self._ffmpeg_proc = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            self._running = True

            # 后台任务
            asyncio.create_task(self._bridge_pcm())
            asyncio.create_task(self._read_mp3_output())
            asyncio.create_task(self._monitor_shairport())
            asyncio.create_task(self._monitor_ffmpeg())

            logger.info("AirPlay 接收器已启动 (shairport-sync): %s (port %d)", self.name, self.port)
            return True

        except Exception as e:
            logger.error("启动 shairport-sync 失败: %s", e)
            return False

    async def _bridge_pcm(self):
        """桥接：读取 shairport-sync stdout → 写入 ffmpeg stdin"""
        try:
            while self._running and self._shairport_proc and self._ffmpeg_proc:
                data = await self._shairport_proc.stdout.read(8192)
                if not data:
                    break
                self._ffmpeg_proc.stdin.write(data)
                await self._ffmpeg_proc.stdin.drain()
        except Exception as e:
            logger.debug("bridge_pcm: %s", e)
        finally:
            if self._ffmpeg_proc and self._ffmpeg_proc.stdin:
                try:
                    self._ffmpeg_proc.stdin.close()
                except Exception:
                    pass

    async def _read_mp3_output(self):
        """从 ffmpeg stdout 读取 MP3 数据并回调"""
        logger.info("等待 AirPlay 音频数据...")
        try:
            while self._running and self._ffmpeg_proc:
                chunk = await self._ffmpeg_proc.stdout.read(4096)
                if not chunk:
                    break

                if not self._playing and self._on_play:
                    self._playing = True
                    logger.info("AirPlay 音频流开始")
                    await self._on_play()

                if self._on_audio_data:
                    await self._on_audio_data(chunk)
        except Exception as e:
            logger.debug("read_mp3_output: %s", e)

        if self._playing and self._on_stop:
            self._playing = False
            logger.info("AirPlay 音频流结束")
            await self._on_stop()

    async def _monitor_shairport(self):
        """监控 shairport-sync 的 stderr 输出"""
        if not self._shairport_proc or not self._shairport_proc.stderr:
            return
        try:
            while self._running:
                line = await self._shairport_proc.stderr.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="ignore").strip()
                if msg:
                    logger.debug("shairport-sync: %s", msg)
        except Exception:
            pass

    async def _monitor_ffmpeg(self):
        """监控 ffmpeg 的 stderr 输出"""
        if not self._ffmpeg_proc or not self._ffmpeg_proc.stderr:
            return
        try:
            while self._running:
                line = await self._ffmpeg_proc.stderr.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="ignore").strip()
                if msg:
                    logger.debug("ffmpeg: %s", msg)
        except Exception:
            pass

    # --- mDNS ---

    async def _register_mdns(self):
        """注册 mDNS/Bonjour 服务让 Apple 设备发现"""
        local_ip = self._get_local_ip()
        hw_addr = self._get_hw_addr()

        raop_info = ServiceInfo(
            "_raop._tcp.local.",
            f"{hw_addr}@{self.name}._raop._tcp.local.",
            addresses=[socket.inet_aton(local_ip)],
            port=self.port,
            properties={
                "txtvers": "1",
                "ch": "2",
                "cn": "0,1,2,3",
                "et": "0,3,5",
                "md": "0,1,2",
                "pw": "false",
                "sr": "44100",
                "ss": "16",
                "tp": "UDP",
                "vs": "220.68",
                "vn": "65537",
                "da": "true",
            },
            server=f"{self.name}.local.",
        )

        self._async_zeroconf = AsyncZeroconf()
        await self._async_zeroconf.async_register_service(raop_info)
        logger.info("mDNS 服务已注册: %s (IP: %s)", self.name, local_ip)

    # --- 工具 ---

    @staticmethod
    def _check_shairport_installed() -> bool:
        try:
            result = subprocess.run(
                ["shairport-sync", "--version"],
                capture_output=True, text=True,
            )
            logger.info("shairport-sync 版本: %s", result.stdout.strip() or result.stderr.strip())
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _get_local_ip() -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    @staticmethod
    def _get_hw_addr() -> str:
        """获取 MAC 地址用于 AirPlay 设备标识"""
        import uuid
        mac = uuid.getnode()
        return ":".join(f"{(mac >> (8 * i)) & 0xFF:02x}" for i in range(5, -1, -1))

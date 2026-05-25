"""主服务 - 整合 DLNA 控制、MiService 控制、HTTP 音频服务、AirPlay 接收"""

import asyncio
import logging
import os
import sys

from .config import Config
from .dlna_discover import DLNADiscovery, DiscoveredDevice
from .dlna_controller import DLNAController
from .miio_controller import MiServiceController
from .audio_server import AudioStreamServer
from .airplay_receiver import AirPlayReceiver
from .web_controller import WebController

logger = logging.getLogger(__name__)


class XiaoaiAirPlayService:
    """小爱音箱 DLNA & AirPlay 服务

    播放控制优先级：
    1. DLNA（纯局域网，低延迟）
    2. MiService（云 API，兼容所有型号）
    """

    def __init__(self, config: Config):
        self.config = config
        # Docker 环境通过 HOST_IP 指定宿主机 IP，否则自动检测
        self.local_ip = os.environ.get("HOST_IP") or config.get_local_ip()

        # 组件
        self.discovery = DLNADiscovery(timeout=config.dlna_scan_timeout)
        self.dlna = DLNAController()
        self.miservice = MiServiceController(
            account=config.mi_account,
            password=config.mi_password,
            did=config.mi_did,
        )
        self.audio_server = AudioStreamServer(
            host=config.http_host,
            port=config.http_port,
            music_dir=config.music_dir,
        )
        self.airplay = AirPlayReceiver(
            name=config.airplay_name,
            port=config.airplay_port,
            on_audio_data=self._on_airplay_audio,
            on_play=self._on_airplay_play,
            on_stop=self._on_airplay_stop,
        )

        self.web = WebController(self)

        self._target_device: DiscoveredDevice | None = None
        self._running = False
        self._airplay_active = False  # AirPlay 当前是否在播放
        self._last_play_url: str = ""   # 最近播放的 URL，用于 resume 工作不常时重播

    @property
    def has_player(self) -> bool:
        """是否有可用的播放控制器"""
        return self.dlna.connected or self.miservice.connected

    async def start(self):
        """启动所有服务"""
        logger.info("=" * 50)
        logger.info("小爱音箱 DLNA & AirPlay 服务启动中...")
        logger.info("本机 IP: %s", self.local_ip)
        logger.info("=" * 50)

        # 1. 启动 HTTP 音频服务器 + Web 控制面板
        self.audio_server.on_all_stream_clients_disconnected = self._on_stream_disconnected
        await self.audio_server.start(extra_routes_cb=self.web.register_routes)
        logger.info("音频文件目录: %s", self.config.music_dir)
        files = self.audio_server.list_music_files()
        logger.info("可用音频文件: %d 个", len(files))

        # 2. 尝试 DLNA 连接
        await self._discover_and_connect_dlna()

        # 3. DLNA 不可用时，尝试 MiService
        if not self.dlna.connected:
            await self._connect_miservice()

        # 4. 启动 AirPlay 接收器
        airplay_started = await self.airplay.start_simple()
        if airplay_started:
            logger.info("AirPlay 接收器: %s (port %d)", self.config.airplay_name, self.config.airplay_port)

        self._running = True
        logger.info("")
        logger.info("服务已就绪！")
        logger.info("  控制面板: http://%s:%d", self.local_ip, self.config.http_port)
        logger.info("  AirPlay:  %s", self.config.airplay_name)
        if self.dlna.connected:
            logger.info("  控制方式: DLNA (局域网)")
            logger.info("  目标音箱: %s (%s)", self._target_device.name, self._target_device.ip)
        elif self.miservice.connected:
            logger.info("  控制方式: MiService (云API)")
            logger.info("  目标音箱: %s", self.miservice.device.name)
        else:
            logger.warning("  控制方式: 无 (未连接到任何音箱)")
            logger.warning("  提示: 设置 MI_USER/MI_PASS 环境变量或配置文件以启用 MiService")
        logger.info("")

        # 保持运行
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """停止所有服务"""
        self._running = False
        await self.airplay.stop()
        await self.dlna.disconnect()
        await self.audio_server.stop()
        logger.info("所有服务已停止")

    async def _discover_and_connect_dlna(self):
        """尝试通过 DLNA 发现并连接小爱音箱"""
        device = await self.discovery.find_xiaoai(
            device_name=self.config.target_device_name,
            device_ip=self.config.target_device_ip,
        )

        if device:
            self._target_device = device
            if device.av_transport_supported:
                connected = await self.dlna.connect(device)
                if connected:
                    logger.info("已通过 DLNA 连接到 %s", device.name)
                    return
                else:
                    logger.warning("DLNA 连接失败")
            else:
                logger.warning("设备 %s 不支持 AVTransport", device.name)
        else:
            logger.info("未通过 DLNA 发现音箱")

    async def _connect_miservice(self):
        """尝试通过 MiService 云 API 连接"""
        if not self.config.mi_account:
            logger.info(
                "MiService 未配置。如需使用，请设置环境变量 MI_USER/MI_PASS "
                "或在 config.yaml 中设置 mi_account/mi_password"
            )
            return

        logger.info("尝试通过 MiService 云 API 连接...")
        connected = await self.miservice.connect()
        if connected:
            logger.info("MiService 连接成功")
        else:
            logger.warning("MiService 连接失败")

    async def _play_url(self, url: str, title: str = "Audio") -> bool:
        """通过可用的控制器播放 URL"""
        self._last_play_url = url
        if self.dlna.connected:
            return await self.dlna.play_url(url, title=title)
        elif self.miservice.connected:
            return await self.miservice.play_url(url)
        else:
            logger.error("无可用的播放控制器")
            return False

    async def _stop_play(self) -> bool:
        """停止播放"""
        if self.dlna.connected:
            return await self.dlna.stop()
        elif self.miservice.connected:
            return await self.miservice.stop()
        return False

    async def play_file(self, filename: str):
        """播放指定音频文件"""
        url = self.audio_server.get_file_url(filename, self.local_ip)
        logger.info("播放文件: %s -> %s", filename, url)
        return await self._play_url(url, title=filename)

    async def play_all(self):
        """顺序播放所有音频文件"""
        files = self.audio_server.list_music_files()
        if not files:
            logger.warning("没有可播放的音频文件")
            return

        for f in files:
            logger.info("正在播放: %s", f["name"])
            await self.play_file(f["filename"])
            await self._wait_playback_done()

    async def _wait_playback_done(self, poll_interval: float = 2.0):
        """等待当前曲目播放结束"""
        if self.dlna.connected:
            from .dlna_controller import TransportState
            while self._running:
                info = await self.dlna.get_transport_info()
                if info and info.state in (TransportState.STOPPED, TransportState.NO_MEDIA):
                    break
                await asyncio.sleep(poll_interval)
        elif self.miservice.connected:
            while self._running:
                status = await self.miservice.get_status()
                if status.get("status") in (None, "stopped", "paused"):
                    break
                await asyncio.sleep(poll_interval)

    # --- AirPlay 回调 ---

    async def _on_airplay_audio(self, data: bytes):
        """接收到 AirPlay 音频数据"""
        await self.audio_server.push_stream_data(data)

    async def _on_airplay_play(self):
        """AirPlay 开始播放"""
        logger.info("AirPlay 播放开始，推送流到音箱...")
        self._airplay_active = True
        if self.has_player:
            stream_url = self.audio_server.get_stream_url(self.local_ip)
            await self._play_url(stream_url, title="AirPlay Stream")

    async def _on_airplay_stop(self):
        """AirPlay 停止播放"""
        logger.info("AirPlay 播放停止")
        self._airplay_active = False
        await self._stop_play()

    async def _on_stream_disconnected(self):
        """流客户端全部断开（如语音唤醒导致音箱中断拉流）"""
        if not self._airplay_active:
            return
        logger.info("流连接断开，等待音箱完成语音应答...")
        await self._wait_speaker_idle()
        if self._airplay_active and self.has_player:
            stream_url = self.audio_server.get_stream_url(self.local_ip)
            logger.info("音箱已空闲，重新推送 AirPlay 流: %s", stream_url)
            await self._play_url(stream_url, title="AirPlay Stream")

    async def _wait_speaker_idle(self, max_wait: float = 60.0, poll_interval: float = 1.0):
        """两阶段轮询音箱状态，确保 TTS 完整播完后再恢复"""
        import json

        async def _speaker_status() -> int:
            try:
                result = await self.miservice.get_status()
                info = result.get("data", {}).get("info", "{}")
                if isinstance(info, str):
                    info = json.loads(info)
                return info.get("status", 0)
            except Exception:
                return -1

        # 第一阶段：等待 TTS 开始（最多 5 秒）
        # 音箱断开 HTTP 流后语音应答可能还没开始播放
        tts_started = False
        for _ in range(5):
            if not self._airplay_active:
                return
            await asyncio.sleep(1.0)
            st = await _speaker_status()
            logger.debug("第一阶段 - 音箱状态: %d", st)
            if st == 1:
                tts_started = True
                logger.info("检测到 TTS 开始播放")
                break

        if not tts_started:
            logger.info("音箱未检测到 TTS，可能是短应答，等待 1.5 秒后恢复")
            await asyncio.sleep(1.5)
            return

        # 第二阶段：等待 TTS 结束
        elapsed = 0.0
        while elapsed < max_wait:
            if not self._airplay_active:
                return
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            st = await _speaker_status()
            logger.debug("第二阶段 - 音箱状态: %d (已等 %.1f s)", st, elapsed)
            if st != 1:  # 0=停止 2=暂停 -1=查询失败
                # 应答结束，补内2 秒缓冲让音箱完全退出 TTS 模式
                await asyncio.sleep(2.0)
                break
        logger.info("TTS 结束 (第二阶段等待了 %.1f 秒)", elapsed)

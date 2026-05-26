"""Web 控制器 - 提供 Web 页面和 REST API 控制小爱音箱"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from aiohttp import web

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class WebController:
    """Web 控制面板，提供页面和 API"""

    def __init__(self, service):
        """
        Args:
            service: XiaoaiAirPlayService 实例
        """
        self.service = service

    def register_routes(self, app: web.Application):
        """注册路由到 aiohttp app"""
        # 页面
        app.router.add_get("/", self._handle_index)
        # API
        app.router.add_get("/api/status", self._handle_api_status)
        app.router.add_get("/api/files", self._handle_api_files)
        app.router.add_post("/api/play", self._handle_api_play)
        app.router.add_post("/api/stop", self._handle_api_stop)
        app.router.add_post("/api/pause", self._handle_api_pause)
        app.router.add_post("/api/resume", self._handle_api_resume)
        app.router.add_post("/api/volume", self._handle_api_volume)
        app.router.add_post("/api/tts", self._handle_api_tts)
        app.router.add_post("/api/upload", self._handle_api_upload)
        app.router.add_delete("/api/files/{filename:.*}", self._handle_api_delete)

    # --- 页面 ---

    async def _handle_index(self, request: web.Request) -> web.Response:
        """Web 控制面板首页"""
        html_path = STATIC_DIR / "index.html"
        if html_path.is_file():
            return web.FileResponse(html_path)
        # fallback: 内嵌 HTML
        return web.Response(text=self._get_embedded_html(), content_type="text/html")

    # --- API ---

    async def _handle_api_status(self, request: web.Request) -> web.Response:
        """获取当前状态"""
        svc = self.service
        status = {
            "connected": svc.has_player,
            "control_mode": "dlna" if svc.dlna.connected else (
                "miservice" if svc.miservice.connected else "none"
            ),
            "device_name": "",
            "device_ip": "",
            "local_ip": svc.local_ip,
            "http_port": svc.config.http_port,
            "airplay_name": svc.config.airplay_name,
            "play_source": svc._play_source,  # "web" | "airplay" | ""
        }
        if svc.dlna.connected and svc._target_device:
            status["device_name"] = svc._target_device.name
            status["device_ip"] = svc._target_device.ip
        elif svc.miservice.connected and svc.miservice.device:
            status["device_name"] = svc.miservice.device.name

        # 获取音箱播放状态
        if svc.miservice.connected:
            try:
                player_status = await svc.miservice.get_status()
                info = player_status.get("data", {}).get("info", "{}")
                if isinstance(info, str):
                    import json
                    info = json.loads(info)
                status["player"] = info
            except Exception:
                status["player"] = {}

        return web.json_response(status)

    async def _handle_api_files(self, request: web.Request) -> web.Response:
        """获取音乐文件列表"""
        files = self.service.audio_server.list_music_files()
        return web.json_response(files)

    async def _handle_api_play(self, request: web.Request) -> web.Response:
        """播放文件或 URL"""
        try:
            data = await request.json()
        except Exception:
            data = {}

        filename = data.get("filename")
        url = data.get("url")

        if filename:
            result = await self.service.play_file(filename)
            return web.json_response({"ok": result, "action": "play", "filename": filename})
        elif url:
            result = await self.service._play_url(url, title="Web Play")
            return web.json_response({"ok": result, "action": "play", "url": url})
        else:
            return web.json_response({"ok": False, "error": "需要 filename 或 url"}, status=400)

    async def _handle_api_stop(self, request: web.Request) -> web.Response:
        """停止播放"""
        result = await self.service._stop_play()
        return web.json_response({"ok": result, "action": "stop"})

    async def _handle_api_pause(self, request: web.Request) -> web.Response:
        """暂停播放"""
        if self.service.miservice.connected:
            result = await self.service.miservice.pause()
            return web.json_response({"ok": result, "action": "pause"})
        elif self.service.dlna.connected:
            result = await self.service.dlna.pause()
            return web.json_response({"ok": result, "action": "pause"})
        return web.json_response({"ok": False, "error": "未连接"}, status=503)

    async def _handle_api_resume(self, request: web.Request) -> web.Response:
        """继续播放（如果 player_play 不支持，回退到重播最近的 URL）"""
        if self.service.miservice.connected:
            result = await self.service.miservice.resume()
            if not result and self.service._last_play_url:
                # 流播放场景下 player_play 可能不支持，重下 play_url
                logger.info("resume 失败，重播上次 URL: %s", self.service._last_play_url)
                result = await self.service._play_url(self.service._last_play_url, title="Resume")
            return web.json_response({"ok": result, "action": "resume"})
        elif self.service.dlna.connected:
            result = await self.service.dlna.play()
            return web.json_response({"ok": result, "action": "resume"})
        return web.json_response({"ok": False, "error": "未连接"}, status=503)

    async def _handle_api_volume(self, request: web.Request) -> web.Response:
        """设置音量"""
        try:
            data = await request.json()
            volume = int(data.get("volume", 50))
        except Exception:
            return web.json_response({"ok": False, "error": "需要 volume (0-100)"}, status=400)

        if self.service.miservice.connected:
            result = await self.service.miservice.set_volume(volume)
            return web.json_response({"ok": result, "action": "volume", "volume": volume})
        else:
            return web.json_response({"ok": False, "error": "MiService 未连接"}, status=503)

    async def _handle_api_tts(self, request: web.Request) -> web.Response:
        """文字转语音"""
        try:
            data = await request.json()
            text = data.get("text", "")
        except Exception:
            return web.json_response({"ok": False, "error": "需要 text"}, status=400)

        if not text:
            return web.json_response({"ok": False, "error": "text 不能为空"}, status=400)

        if self.service.miservice.connected:
            result = await self.service.miservice.tts(text)
            return web.json_response({"ok": result, "action": "tts", "text": text})
        else:
            return web.json_response({"ok": False, "error": "MiService 未连接"}, status=503)

    async def _handle_api_upload(self, request: web.Request) -> web.Response:
        """上传音频文件"""
        reader = await request.multipart()
        field = await reader.next()

        if not field or field.name != "file":
            return web.json_response({"ok": False, "error": "需要 file 字段"}, status=400)

        filename = field.filename
        if not filename:
            return web.json_response({"ok": False, "error": "文件名为空"}, status=400)

        # 安全性：只允许音频文件
        ext = Path(filename).suffix.lower()
        allowed = {".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg", ".wma"}
        if ext not in allowed:
            return web.json_response(
                {"ok": False, "error": f"不支持的格式: {ext}，允许: {', '.join(allowed)}"},
                status=400,
            )

        save_path = self.service.audio_server.music_dir / filename
        size = 0
        with open(save_path, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
                size += len(chunk)

        logger.info("文件上传完成: %s (%d bytes)", filename, size)
        return web.json_response({
            "ok": True,
            "action": "upload",
            "filename": filename,
            "size": size,
        })

    async def _handle_api_delete(self, request: web.Request) -> web.Response:
        """删除音频文件"""
        filename = request.match_info["filename"]
        filepath = self.service.audio_server.music_dir / filename

        try:
            filepath.resolve().relative_to(self.service.audio_server.music_dir)
        except ValueError:
            return web.json_response({"ok": False, "error": "路径不合法"}, status=403)

        if not filepath.is_file():
            return web.json_response({"ok": False, "error": "文件不存在"}, status=404)

        filepath.unlink()
        logger.info("文件已删除: %s", filename)
        return web.json_response({"ok": True, "action": "delete", "filename": filename})

    def _get_embedded_html(self) -> str:
        """内嵌 HTML（当 static/index.html 不存在时的 fallback）"""
        return "<html><body><h1>小爱音箱控制面板</h1><p>请创建 static/index.html</p></body></html>"

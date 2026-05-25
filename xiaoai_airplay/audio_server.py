"""HTTP 音频流服务器 - 提供本地音频文件和实时流"""

import asyncio
import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

from aiohttp import web

logger = logging.getLogger(__name__)

MIME_MAP = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".wma": "audio/x-ms-wma",
}


class AudioStreamServer:
    """HTTP 音频服务器，支持文件服务和实时流"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, music_dir: str = "./music"):
        self.host = host
        self.port = port
        self.music_dir = Path(music_dir).resolve()
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # 实时流缓冲区（用于 AirPlay 转发）
        self._stream_buffer = asyncio.Queue(maxsize=1000)
        self._stream_active = False
        self._stream_clients: list[asyncio.Queue] = []

    @property
    def app(self) -> Optional[web.Application]:
        return self._app

    async def start(self, extra_routes_cb=None):
        """启动 HTTP 服务器

        Args:
            extra_routes_cb: 可选回调，接收 app 用于注册额外路由（如 Web 控制面板）
        """
        self.music_dir.mkdir(parents=True, exist_ok=True)

        self._app = web.Application(client_max_size=200 * 1024 * 1024)  # 200MB 上传限制
        self._app.router.add_get("/file/{filename:.*}", self._handle_file)
        self._app.router.add_get("/stream", self._handle_stream)
        self._app.router.add_get("/list", self._handle_list)

        # 注册额外路由（Web 控制面板等）
        if extra_routes_cb:
            extra_routes_cb(self._app)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        logger.info("HTTP 音频服务器已启动: http://%s:%d", self.host, self.port)

    async def stop(self):
        """停止 HTTP 服务器"""
        if self._runner:
            await self._runner.cleanup()
        logger.info("HTTP 音频服务器已停止")

    def get_file_url(self, filename: str, local_ip: str) -> str:
        """获取文件的完整 URL"""
        from urllib.parse import quote
        return f"http://{local_ip}:{self.port}/file/{quote(filename)}"

    def get_stream_url(self, local_ip: str) -> str:
        """获取实时流的 URL"""
        return f"http://{local_ip}:{self.port}/stream"

    async def push_stream_data(self, data: bytes):
        """向流缓冲区写入音频数据（由 AirPlay 接收器调用）"""
        for client_queue in self._stream_clients:
            try:
                client_queue.put_nowait(data)
            except asyncio.QueueFull:
                # 丢弃旧数据，防止阻塞
                try:
                    client_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                client_queue.put_nowait(data)

    def list_music_files(self) -> list[dict]:
        """列出可用的音频文件"""
        files = []
        for f in sorted(self.music_dir.rglob("*")):
            if f.is_file() and f.suffix.lower() in MIME_MAP:
                rel_path = f.relative_to(self.music_dir)
                files.append({
                    "name": f.stem,
                    "filename": str(rel_path),
                    "size": f.stat().st_size,
                    "mime": MIME_MAP.get(f.suffix.lower(), "audio/mpeg"),
                })
        return files

    # --- HTTP Handlers ---

    async def _handle_list(self, request: web.Request) -> web.Response:
        """返回音频文件列表 JSON"""
        files = self.list_music_files()
        return web.json_response(files)

    async def _handle_file(self, request: web.Request) -> web.StreamResponse:
        """提供音频文件（支持 Range 请求）"""
        filename = request.match_info["filename"]
        filepath = self.music_dir / filename

        if not filepath.is_file():
            raise web.HTTPNotFound(text=f"文件不存在: {filename}")

        # 安全检查
        try:
            filepath.resolve().relative_to(self.music_dir)
        except ValueError:
            raise web.HTTPForbidden(text="路径不合法")

        file_size = filepath.stat().st_size
        mime = MIME_MAP.get(filepath.suffix.lower(), "application/octet-stream")

        # 处理 Range 请求
        range_header = request.headers.get("Range")
        if range_header:
            return await self._handle_range_request(request, filepath, file_size, mime, range_header)

        # 完整文件响应
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": mime,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
            },
        )
        await response.prepare(request)

        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                await response.write(chunk)

        return response

    async def _handle_range_request(
        self, request: web.Request, filepath: Path, file_size: int, mime: str, range_header: str
    ) -> web.StreamResponse:
        """处理 Range 请求（音频 seek）"""
        try:
            range_spec = range_header.replace("bytes=", "")
            parts = range_spec.split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else file_size - 1
        except (ValueError, IndexError):
            raise web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{file_size}"}
            )

        if start >= file_size:
            raise web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{file_size}"}
            )

        end = min(end, file_size - 1)
        content_length = end - start + 1

        response = web.StreamResponse(
            status=206,
            headers={
                "Content-Type": mime,
                "Content-Length": str(content_length),
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
            },
        )
        await response.prepare(request)

        with open(filepath, "rb") as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk_size = min(65536, remaining)
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                await response.write(chunk)
                remaining -= len(chunk)

        return response

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        """提供实时音频流（chunked transfer，用于 AirPlay 转发）"""
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "audio/mpeg",
                "Transfer-Encoding": "chunked",
                "Cache-Control": "no-cache",
            },
        )
        await response.prepare(request)

        client_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._stream_clients.append(client_queue)
        logger.info("新的流客户端已连接，当前客户端数: %d", len(self._stream_clients))

        try:
            while True:
                try:
                    data = await asyncio.wait_for(client_queue.get(), timeout=30.0)
                    await response.write(data)
                except asyncio.TimeoutError:
                    # 发送空数据保持连接
                    await response.write(b"")
                except ConnectionResetError:
                    break
        finally:
            self._stream_clients.remove(client_queue)
            logger.info("流客户端已断开，剩余客户端数: %d", len(self._stream_clients))

        return response

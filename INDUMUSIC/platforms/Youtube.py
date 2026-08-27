# -*- coding: utf-8 -*-
"""YouTube search, metadata, download and streaming helpers for INDU MUSIC."""

import asyncio
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs, urlparse

import aiofiles
import aiohttp
import yt_dlp
from pyrogram.enums import MessageEntityType
from pyrogram.types import Message
from py_yt import VideosSearch

from INDUMUSIC import LOGGER
from INDUMUSIC.utils.cookie_handler import COOKIE_PATH
from INDUMUSIC.utils.downloader import download_audio_concurrent, yt_dlp_download
from INDUMUSIC.utils.errors import capture_internal_err
from INDUMUSIC.utils.formatters import time_to_seconds
from INDUMUSIC.utils.tuning import YTDLP_TIMEOUT, YOUTUBE_META_MAX, YOUTUBE_META_TTL


_module_logger = LOGGER(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
# Keep secrets in Render Environment Variables instead of hard-coding them.
SHRUTI_API_KEY = os.getenv("SHRUTI_API_KEY", "ShrutiBotsL0zQEKsazSrYS2LWsIQW")
PRIMARY_API_URL = os.getenv("PRIMARY_API_URL", "https://api.shrutibots.site").rstrip("/")
FALLBACK_API_URL = os.getenv("FALLBACK_API_URL", "http://13.212.126.0:2020").rstrip("/")

PRIMARY_API_LOADED = False
FALLBACK_API_LOADED = False

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

_cache: Dict[str, Tuple[float, List[Dict]]] = {}
_cache_lock = asyncio.Lock()
_formats_cache: Dict[str, Tuple[float, List[Dict], str]] = {}
_formats_lock = asyncio.Lock()

_request_timestamps: List[float] = []
_RATE_LIMIT_WINDOW = 60.0
_MAX_REQUESTS_PER_WINDOW = 10
_rate_limit_lock = asyncio.Lock()

_yt_session: Optional[aiohttp.ClientSession] = None
_yt_session_lock = asyncio.Lock()


async def _check_rate_limit_async() -> None:
    """Rate-limit yt-dlp/API requests without blocking the event loop."""
    global _request_timestamps
    while True:
        async with _rate_limit_lock:
            now = time.monotonic()
            _request_timestamps = [
                ts for ts in _request_timestamps
                if now - ts < _RATE_LIMIT_WINDOW
            ]
            if len(_request_timestamps) < _MAX_REQUESTS_PER_WINDOW:
                _request_timestamps.append(now)
                return
            wait_for = max(0.1, _RATE_LIMIT_WINDOW - (now - _request_timestamps[0]))
        await asyncio.sleep(wait_for)


async def _get_yt_session() -> aiohttp.ClientSession:
    """Return one reusable aiohttp session, created lazily inside the running loop."""
    global _yt_session
    if _yt_session is not None and not _yt_session.closed:
        return _yt_session

    async with _yt_session_lock:
        if _yt_session is not None and not _yt_session.closed:
            return _yt_session
        connector = aiohttp.TCPConnector(
            limit=32,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=300, sock_connect=15, sock_read=90)
        _yt_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return _yt_session


async def close_youtube_session() -> None:
    """Close the shared HTTP session when the application shuts down."""
    global _yt_session
    if _yt_session is not None and not _yt_session.closed:
        await _yt_session.close()
    _yt_session = None


async def load_apis() -> Tuple[bool, bool]:
    """Check configured download APIs. Failure here never prevents bot startup."""
    global PRIMARY_API_LOADED, FALLBACK_API_LOADED
    PRIMARY_API_LOADED = False
    FALLBACK_API_LOADED = False

    session = await _get_yt_session()

    if PRIMARY_API_URL:
        try:
            async with session.get(
                f"{PRIMARY_API_URL}/",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                PRIMARY_API_LOADED = response.status < 500
                if PRIMARY_API_LOADED:
                    _module_logger.info("Primary YouTube API reachable")
                else:
                    _module_logger.warning("Primary YouTube API returned %s", response.status)
        except Exception as exc:
            _module_logger.warning("Primary YouTube API unavailable: %s", exc)

    if FALLBACK_API_URL:
        try:
            async with session.get(
                f"{FALLBACK_API_URL}/",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as response:
                FALLBACK_API_LOADED = response.status < 500
                if FALLBACK_API_LOADED:
                    _module_logger.info("Fallback YouTube API reachable")
                else:
                    _module_logger.warning("Fallback YouTube API returned %s", response.status)
        except Exception as exc:
            _module_logger.warning("Fallback YouTube API unavailable: %s", exc)

    return PRIMARY_API_LOADED, FALLBACK_API_LOADED


def _cookiefile_path() -> Optional[str]:
    try:
        path = str(COOKIE_PATH or "").strip()
        if path and os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    except Exception:
        pass
    return None


def _cookies_args() -> List[str]:
    path = _cookiefile_path()
    return ["--cookies", path] if path else []


def _ytdlp_command(*args: str) -> List[str]:
    """Use the installed yt-dlp module, avoiding PATH/entrypoint issues on Render."""
    return [sys.executable, "-m", "yt_dlp", *args]


async def _exec_proc(*args: str, timeout: Optional[float] = None) -> Tuple[bytes, bytes, int]:
    """Execute a subprocess safely and return stdout, stderr and exit code."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    limit = timeout or YTDLP_TIMEOUT
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=limit)
        return stdout, stderr, proc.returncode or 0
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        return b"", b"timeout", -1


def _video_id(link: str) -> Optional[str]:
    """Extract a YouTube video ID from common URL forms or a bare ID."""
    value = str(link or "").strip()
    if not value:
        return None

    parsed = urlparse(value)
    host = parsed.netloc.lower().split(":")[0]
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate or None
    if "youtube.com" in host:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
            return candidate
        for prefix in ("/shorts/", "/live/", "/embed/"):
            if parsed.path.startswith(prefix):
                candidate = parsed.path[len(prefix):].split("/")[0]
                return candidate or None

    # Bare video IDs are normally 11 characters. Keep this permissive because
    # some internal callers pass an ID-like value directly.
    if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", value):
        return value
    return None


async def _download_response_to_file(
    response: aiohttp.ClientResponse,
    file_path: str,
) -> Optional[str]:
    """Save a successful API response, rejecting JSON/error bodies."""
    if response.status != 200:
        return None

    content_type = (response.headers.get("Content-Type") or "").lower()
    if "json" in content_type or "text/html" in content_type:
        return None

    tmp_path = f"{file_path}.part"
    try:
        async with aiofiles.open(tmp_path, "wb") as output:
            async for chunk in response.content.iter_chunked(1024 * 1024):
                if chunk:
                    await output.write(chunk)
        if os.path.getsize(tmp_path) <= 10240:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp_path)
            return None
        os.replace(tmp_path, file_path)
        return file_path
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_path)
        with contextlib.suppress(FileNotFoundError):
            os.remove(file_path)
        return None


# ---------------------------------------------------------------------------
# API downloaders
# ---------------------------------------------------------------------------
async def download_song_primary_api(link: str) -> Optional[str]:
    if not PRIMARY_API_URL or not SHRUTI_API_KEY:
        return None
    video_id = _video_id(link)
    if not video_id:
        return None

    file_path = str(DOWNLOAD_DIR / f"{video_id}.mp3")
    if os.path.isfile(file_path) and os.path.getsize(file_path) > 10240:
        return file_path

    try:
        session = await _get_yt_session()
        params = {"url": video_id, "type": "audio", "api_key": SHRUTI_API_KEY}
        async with session.get(
            f"{PRIMARY_API_URL}/download",
            params=params,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as response:
            return await _download_response_to_file(response, file_path)
    except Exception as exc:
        _module_logger.warning("Primary audio API failed: %s", exc)
        return None


async def download_video_primary_api(link: str) -> Optional[str]:
    if not PRIMARY_API_URL or not SHRUTI_API_KEY:
        return None
    video_id = _video_id(link)
    if not video_id:
        return None

    file_path = str(DOWNLOAD_DIR / f"{video_id}.mp4")
    if os.path.isfile(file_path) and os.path.getsize(file_path) > 10240:
        return file_path

    try:
        session = await _get_yt_session()
        params = {"url": video_id, "type": "video", "api_key": SHRUTI_API_KEY}
        async with session.get(
            f"{PRIMARY_API_URL}/download",
            params=params,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as response:
            return await _download_response_to_file(response, file_path)
    except Exception as exc:
        _module_logger.warning("Primary video API failed: %s", exc)
        return None


async def download_song_fallback_api(link: str) -> Optional[str]:
    if not FALLBACK_API_URL:
        return None
    video_id = _video_id(link)
    if not video_id:
        return None

    file_path = str(DOWNLOAD_DIR / f"{video_id}.mp3")
    if os.path.isfile(file_path) and os.path.getsize(file_path) > 10240:
        return file_path

    try:
        session = await _get_yt_session()
        async with session.get(
            f"{FALLBACK_API_URL}/download",
            params={"url": video_id, "type": "audio"},
            timeout=aiohttp.ClientTimeout(total=45),
        ) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)

        token = data.get("download_token")
        direct_url = data.get("url") or data.get("download_url")
        if not token and not direct_url:
            return None

        if direct_url:
            stream_url = direct_url
            headers = {}
        else:
            stream_url = f"{FALLBACK_API_URL}/stream/{video_id}"
            headers = {"X-Download-Token": token}

        async with session.get(
            stream_url,
            params=None if direct_url else {"type": "audio"},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as file_response:
            return await _download_response_to_file(file_response, file_path)
    except Exception as exc:
        _module_logger.warning("Fallback audio API failed: %s", exc)
        return None


async def download_video_fallback_api(link: str) -> Optional[str]:
    if not FALLBACK_API_URL:
        return None
    video_id = _video_id(link)
    if not video_id:
        return None

    file_path = str(DOWNLOAD_DIR / f"{video_id}.mp4")
    if os.path.isfile(file_path) and os.path.getsize(file_path) > 10240:
        return file_path

    try:
        session = await _get_yt_session()
        async with session.get(
            f"{FALLBACK_API_URL}/download",
            params={"url": video_id, "type": "video"},
            timeout=aiohttp.ClientTimeout(total=45),
        ) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)

        token = data.get("download_token")
        direct_url = data.get("url") or data.get("download_url")
        if not token and not direct_url:
            return None

        if direct_url:
            stream_url = direct_url
            headers = {}
        else:
            stream_url = f"{FALLBACK_API_URL}/stream/{video_id}"
            headers = {"X-Download-Token": token}

        async with session.get(
            stream_url,
            params=None if direct_url else {"type": "video"},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=600),
        ) as file_response:
            return await _download_response_to_file(file_response, file_path)
    except Exception as exc:
        _module_logger.warning("Fallback video API failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# yt-dlp downloaders
# ---------------------------------------------------------------------------
async def download_video_ytdlp(link: str) -> Optional[str]:
    video_id = _video_id(link)
    if not video_id:
        return None
    file_path = str(DOWNLOAD_DIR / f"{video_id}.mp4")
    if os.path.isfile(file_path) and os.path.getsize(file_path) > 10240:
        return file_path

    await _check_rate_limit_async()
    formats = [
        "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]",
        "best[ext=mp4]/best",
        "worst[ext=mp4]/worst",
    ]
    for fmt in formats:
        args = _ytdlp_command(
            *(_cookies_args()),
            "--no-warnings",
            "--no-playlist",
            "--geo-bypass",
            "--force-ipv4",
            "-f", fmt,
            "-o", file_path,
            link,
        )
        stdout, stderr, code = await _exec_proc(*args, timeout=max(YTDLP_TIMEOUT, 120))
        if os.path.isfile(file_path) and os.path.getsize(file_path) > 10240:
            return file_path
        _module_logger.warning("yt-dlp video attempt failed (%s): %s", code, stderr.decode(errors="ignore")[-500:])
        await asyncio.sleep(1)
    return None


async def download_audio_ytdlp(link: str) -> Optional[str]:
    video_id = _video_id(link)
    if not video_id:
        return None

    # Do not use --extract-audio here: native Render Python services often do
    # not have the ffmpeg binary installed. A bestaudio file (m4a/webm) is
    # already playable by the voice-chat stack and avoids an unnecessary
    # conversion step.
    cached = list(DOWNLOAD_DIR.glob(f"{video_id}.*"))
    for candidate in cached:
        if candidate.suffix.lower() in {".webm", ".m4a", ".mp3", ".opus", ".aac"}:
            try:
                if candidate.stat().st_size > 10240:
                    return str(candidate)
            except OSError:
                pass

    await _check_rate_limit_async()
    output_template = str(DOWNLOAD_DIR / f"{video_id}.%(ext)s")
    args = _ytdlp_command(
        *(_cookies_args()),
        "--no-warnings",
        "--no-playlist",
        "--geo-bypass",
        "--force-ipv4",
        "-f", "bestaudio/best",
        "-o", output_template,
        link,
    )
    stdout, stderr, code = await _exec_proc(*args, timeout=max(YTDLP_TIMEOUT, 120))
    if code == 0:
        candidates = []
        for candidate in DOWNLOAD_DIR.glob(f"{video_id}.*"):
            if candidate.suffix.lower() in {".webm", ".m4a", ".mp3", ".opus", ".aac"}:
                try:
                    if candidate.stat().st_size > 10240:
                        candidates.append(candidate)
                except OSError:
                    pass
        if candidates:
            newest = max(candidates, key=lambda item: item.stat().st_mtime)
            return str(newest)
    _module_logger.warning("yt-dlp audio failed (%s): %s", code, stderr.decode(errors="ignore")[-800:])
    return None


async def download_audio(link: str) -> Optional[str]:
    """Audio fallback chain: Primary API -> Fallback API -> yt-dlp."""
    result = await download_song_primary_api(link)
    if result:
        return result
    result = await download_song_fallback_api(link)
    if result:
        return result
    return await download_audio_ytdlp(link)


async def download_video(link: str) -> Optional[str]:
    """Video fallback chain: Primary API -> Fallback API -> yt-dlp."""
    result = await download_video_primary_api(link)
    if result:
        return result
    result = await download_video_fallback_api(link)
    if result:
        return result
    return await download_video_ytdlp(link)


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------
@capture_internal_err
async def cached_youtube_search(query: str) -> List[Dict]:
    key = f"q:{query.strip().lower()}"
    now = time.monotonic()
    async with _cache_lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < YOUTUBE_META_TTL:
            return cached[1]
        _cache.pop(key, None)
        if len(_cache) >= YOUTUBE_META_MAX:
            _cache.clear()

    try:
        data = await VideosSearch(query, limit=1).next()
        result = data.get("result", []) or []
    except Exception as exc:
        _module_logger.warning("YouTube search failed: %s", exc)
        result = []

    if result:
        async with _cache_lock:
            _cache[key] = (now, result)
    return result


@capture_internal_err
async def youtube_search_multi(query: str, limit: int = 8) -> List[Dict]:
    limit = max(1, min(int(limit), 20))
    try:
        data = await VideosSearch(query, limit=limit).next()
        return data.get("result", []) or []
    except Exception as exc:
        _module_logger.warning("YouTube multi-search failed: %s", exc)
        return []


class YouTubeAPI:
    def __init__(self) -> None:
        self.base_url = "https://www.youtube.com/watch?v="
        self.playlist_url = "https://www.youtube.com/playlist?list="
        self.status = "https://www.youtube.com/oembed?url="
        self._url_pattern = re.compile(r"(?:youtube\.com|youtu\.be)", re.I)
        self.reg = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    def _prepare_link(
        self,
        link: str,
        videoid: Union[str, bool, None] = None,
    ) -> str:
        if isinstance(videoid, str) and videoid.strip():
            return f"{self.base_url}{videoid.strip()}"

        value = str(link or "").strip()
        if not value:
            return value

        vid = _video_id(value)
        if vid and ("youtube.com" in value.lower() or "youtu.be" in value.lower()):
            return f"{self.base_url}{vid}"
        return value

    @capture_internal_err
    async def url(self, message: Message) -> Optional[str]:
        messages = [message]
        if message.reply_to_message:
            messages.append(message.reply_to_message)

        for msg in messages:
            text = msg.text or msg.caption or ""
            entities = msg.entities or msg.caption_entities or []
            for ent in entities:
                if ent.type == MessageEntityType.URL:
                    found = text[ent.offset: ent.offset + ent.length]
                    if self._url_pattern.search(found):
                        return found
                elif ent.type == MessageEntityType.TEXT_LINK:
                    found = ent.url or ""
                    if self._url_pattern.search(found):
                        return found

            # Also handle plain YouTube URLs when Telegram did not create an entity.
            match = re.search(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/[^\s<>]+", text, re.I)
            if match:
                return match.group(0).rstrip(".,!?)]")
        return None

    @capture_internal_err
    async def exists(self, link: str, videoid: Union[str, bool, None] = None) -> bool:
        prepared = self._prepare_link(link, videoid)
        return bool(self._url_pattern.search(prepared)) and bool(_video_id(prepared))

    @capture_internal_err
    async def _fetch_video_info(self, query: str, *, use_cache: bool = True) -> Optional[Dict]:
        prepared = self._prepare_link(query)
        if use_cache and not prepared.lower().startswith(("http://", "https://")):
            results = await cached_youtube_search(prepared)
            return results[0] if results else None

        # yt-dlp is more reliable than search for direct URLs and avoids py_yt
        # failing on YouTube changes.
        if prepared.lower().startswith(("http://", "https://")):
            await _check_rate_limit_async()
            stdout, stderr, code = await _exec_proc(
                *_ytdlp_command(*(_cookies_args()), "--dump-single-json", "--no-playlist", "--skip-download", prepared),
                timeout=max(YTDLP_TIMEOUT, 60),
            )
            if stdout:
                try:
                    return json.loads(stdout.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    pass
            _module_logger.warning("yt-dlp metadata failed (%s): %s", code, stderr.decode(errors="ignore")[-500:])
            return None

        results = await cached_youtube_search(prepared) if use_cache else []
        if results:
            return results[0]
        try:
            data = await VideosSearch(prepared, limit=1).next()
            results = data.get("result", []) or []
            return results[0] if results else None
        except Exception:
            return None

    @capture_internal_err
    async def is_live(self, link: str) -> bool:
        prepared = self._prepare_link(link)
        await _check_rate_limit_async()
        stdout, _, _ = await _exec_proc(
            *_ytdlp_command(*(_cookies_args()), "--dump-single-json", "--no-playlist", "--skip-download", prepared),
            timeout=max(YTDLP_TIMEOUT, 60),
        )
        if not stdout:
            return False
        try:
            return bool(json.loads(stdout.decode("utf-8", errors="replace")).get("is_live"))
        except (json.JSONDecodeError, TypeError):
            return False

    @capture_internal_err
    async def details(
        self,
        link: str,
        videoid: Union[str, bool, None] = None,
    ) -> Tuple[str, Optional[str], int, str, str]:
        info = await self._fetch_video_info(self._prepare_link(link, videoid))
        if not info:
            raise ValueError("Video not found")
        duration = info.get("duration")
        if isinstance(duration, (int, float)):
            duration_seconds = int(duration)
            duration_text = f"{duration_seconds // 60}:{duration_seconds % 60:02d}"
        else:
            duration_text = duration
            duration_seconds = int(time_to_seconds(duration)) if duration else 0
        thumbnails = info.get("thumbnails") or []
        thumb = info.get("thumbnail") or (thumbnails[0].get("url") if thumbnails else "")
        return info.get("title", ""), duration_text, duration_seconds, (thumb or "").split("?")[0], info.get("id", "")

    @capture_internal_err
    async def title(self, link: str, videoid: Union[str, bool, None] = None) -> str:
        info = await self._fetch_video_info(self._prepare_link(link, videoid))
        return info.get("title", "") if info else ""

    @capture_internal_err
    async def duration(self, link: str, videoid: Union[str, bool, None] = None) -> Optional[str]:
        info = await self._fetch_video_info(self._prepare_link(link, videoid))
        if not info:
            return None
        duration = info.get("duration")
        if isinstance(duration, (int, float)):
            seconds = int(duration)
            return f"{seconds // 60}:{seconds % 60:02d}"
        return duration

    @capture_internal_err
    async def thumbnail(self, link: str, videoid: Union[str, bool, None] = None) -> str:
        info = await self._fetch_video_info(self._prepare_link(link, videoid))
        if not info:
            return ""
        thumbnails = info.get("thumbnails") or []
        thumb = info.get("thumbnail") or (thumbnails[0].get("url") if thumbnails else "")
        return (thumb or "").split("?")[0]

    @capture_internal_err
    async def video(
        self,
        link: str,
        videoid: Union[str, bool, None] = None,
    ) -> Tuple[int, str]:
        prepared = self._prepare_link(link, videoid)

        # Prefer a downloaded file because direct YouTube URLs expire.
        downloaded = await download_video(prepared)
        if downloaded:
            return 1, downloaded

        await _check_rate_limit_async()
        formats = [
            "best[height<=720][ext=mp4]/best[height<=720]/best",
            "worst[ext=mp4]/worst",
        ]
        for fmt in formats:
            stdout, stderr, code = await _exec_proc(
                *_ytdlp_command(*(_cookies_args()), "--no-warnings", "--no-playlist", "--no-check-certificates", "-g", "-f", fmt, prepared),
                timeout=max(YTDLP_TIMEOUT, 60),
            )
            if stdout:
                urls = [line.strip() for line in stdout.decode(errors="replace").splitlines() if line.strip()]
                if urls and all(u.startswith(("http://", "https://")) for u in urls):
                    # If yt-dlp returned separate video/audio URLs, use the first
                    # combined URL when available. PyTgCalls cannot merge them.
                    return 1, urls[0]
            _module_logger.warning("yt-dlp stream attempt failed (%s): %s", code, stderr.decode(errors="ignore")[-500:])
        return 0, "Unable to obtain a playable YouTube stream."

    @capture_internal_err
    async def playlist(
        self,
        link: str,
        limit: int,
        user_id,
        videoid: Union[str, bool, None] = None,
    ) -> List[str]:
        del user_id  # kept for API compatibility
        if videoid:
            link = f"{self.playlist_url}{str(videoid).strip()}"
        link = str(link or "").strip()
        if not link:
            return []

        limit = max(1, min(int(limit), 100))
        await _check_rate_limit_async()
        args = _ytdlp_command(
            *(_cookies_args()),
            "--no-warnings",
            "--flat-playlist",
            "--playlist-end", str(limit),
            "--print", "%(id)s",
            "--skip-download",
            link,
        )
        stdout, stderr, code = await _exec_proc(*args, timeout=max(YTDLP_TIMEOUT, 120))
        if code != 0 and not stdout:
            _module_logger.warning("Playlist fetch failed: %s", stderr.decode(errors="ignore")[-500:])
            return []

        items = []
        for value in stdout.decode(errors="replace").splitlines():
            value = value.strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", value):
                items.append(value)
        return items[:limit]

    @capture_internal_err
    async def track(
        self,
        link: str,
        videoid: Union[str, bool, None] = None,
    ) -> Tuple[Dict, str]:
        prepared = self._prepare_link(link, videoid)
        info = await self._fetch_video_info(prepared)
        if not info:
            raise ValueError("Track not found")

        thumbnails = info.get("thumbnails") or []
        thumb = info.get("thumbnail") or (thumbnails[0].get("url") if thumbnails else "")
        duration = info.get("duration")
        if isinstance(duration, (int, float)):
            seconds = int(duration)
            duration_min = f"{seconds // 60}:{seconds % 60:02d}" if seconds > 0 else None
        else:
            duration_min = duration or None

        vidid = info.get("id", "")
        details = {
            "title": info.get("title", ""),
            "link": info.get("webpage_url") or prepared,
            "vidid": vidid,
            "duration_min": duration_min,
            "thumb": (thumb or "").split("?")[0],
        }
        return details, vidid

    @capture_internal_err
    async def formats(
        self,
        link: str,
        videoid: Union[str, bool, None] = None,
    ) -> Tuple[List[Dict], str]:
        prepared = self._prepare_link(link, videoid)
        key = f"f:{prepared}"
        now = time.monotonic()
        async with _formats_lock:
            cached = _formats_cache.get(key)
            if cached and now - cached[0] < YOUTUBE_META_TTL:
                return cached[1], cached[2]

        await _check_rate_limit_async()

        def extract_formats() -> List[Dict]:
            opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
            cookie_file = _cookiefile_path()
            if cookie_file:
                opts["cookiefile"] = cookie_file
            result: List[Dict] = []
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(prepared, download=False)
                for fmt in info.get("formats", []):
                    size = fmt.get("filesize") or fmt.get("filesize_approx")
                    if not size:
                        continue
                    format_id = fmt.get("format_id")
                    ext = fmt.get("ext")
                    if not format_id or not ext:
                        continue
                    result.append({
                        "format": fmt.get("format") or fmt.get("format_note") or ext,
                        "filesize": size,
                        "format_id": format_id,
                        "ext": ext,
                        "format_note": fmt.get("format_note") or "",
                        "yturl": prepared,
                    })
            return result

        try:
            out = await asyncio.to_thread(extract_formats)
        except Exception as exc:
            _module_logger.warning("YouTube format extraction failed: %s", exc)
            out = []

        async with _formats_lock:
            if len(_formats_cache) >= YOUTUBE_META_MAX:
                _formats_cache.clear()
            _formats_cache[key] = (now, out, prepared)
        return out, prepared

    @capture_internal_err
    async def slider(
        self,
        link: str,
        query_type: int,
        videoid: Union[str, bool, None] = None,
    ) -> Tuple[str, Optional[str], str, str]:
        query_type = int(query_type)
        if query_type < 0:
            raise IndexError("Query type cannot be negative")
        data = await VideosSearch(self._prepare_link(link, videoid), limit=10).next()
        results = data.get("result", []) or []
        if query_type >= len(results):
            raise IndexError(f"Query type index {query_type} out of range (found {len(results)} results)")
        item = results[query_type]
        thumbs = item.get("thumbnails") or []
        thumb = item.get("thumbnail") or (thumbs[0].get("url") if thumbs else "")
        return item.get("title", ""), item.get("duration"), (thumb or "").split("?")[0], item.get("id", "")

    @capture_internal_err
    async def download(
        self,
        link: str,
        mystic,
        *,
        video: Union[bool, str, None] = None,
        videoid: Union[str, bool, None] = None,
        songaudio: Union[bool, str, None] = None,
        songvideo: Union[bool, str, None] = None,
        format_id: Union[bool, str, None] = None,
        title: Union[bool, str, None] = None,
    ) -> Union[Tuple[str, Optional[bool]], Tuple[None, None]]:
        del mystic, songaudio, songvideo, format_id, title  # API compatibility
        prepared = self._prepare_link(link, videoid)
        vid = _video_id(prepared)
        if not vid:
            return None, None

        is_video = bool(video)
        expected_ext = "mp4" if is_video else "mp3"
        common_file_path = str(DOWNLOAD_DIR / f"{vid}.{expected_ext}")
        if os.path.isfile(common_file_path) and os.path.getsize(common_file_path) > 10240:
            return common_file_path, True

        if is_video:
            result = await download_video(prepared)
            if result:
                if result != common_file_path and result.endswith(".mp4"):
                    with contextlib.suppress(Exception):
                        os.replace(result, common_file_path)
                        result = common_file_path
                return result, True

            status, stream_url = await self.video(prepared)
            return (stream_url, None) if status == 1 else (None, None)

        # Audio: race the independent download strategies. Do not duplicate the
        # primary->fallback chain inside another race; this avoids unnecessary
        # API load and prevents cancelled tasks from leaving partial files.
        async def try_api_chain() -> Optional[str]:
            return await download_audio(prepared)

        async def try_project_downloader() -> Optional[str]:
            try:
                return await yt_dlp_download(prepared, type="audio")
            except Exception:
                return None

        async def try_concurrent_downloader() -> Optional[str]:
            try:
                return await download_audio_concurrent(prepared)
            except Exception:
                return None

        tasks = [
            asyncio.create_task(try_api_chain()),
            asyncio.create_task(try_project_downloader()),
            asyncio.create_task(try_concurrent_downloader()),
        ]
        winner: Optional[str] = None
        try:
            for future in asyncio.as_completed(tasks):
                try:
                    result = await future
                except asyncio.CancelledError:
                    continue
                except Exception:
                    continue
                if result and os.path.isfile(result) and os.path.getsize(result) > 10240:
                    winner = result
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if not winner:
            return None, None

        # Keep the downloader's real extension. Renaming an m4a/webm stream
        # to .mp3/.webm without transcoding corrupts the file's container.
        return winner, True


YouTube = YouTubeAPI()

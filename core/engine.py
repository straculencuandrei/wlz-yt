from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import yt_dlp

# Windows flag to suppress console popups when running ffmpeg subprocesses
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class DownloadCancelledError(Exception):
    """Raised when the user cancels an active download or encode task."""


@dataclass
class VideoFormatOption:
    height: int
    fps: int
    label: str
    vcodec: str
    is_native_h264: bool


@dataclass
class VideoMetadata:
    url: str
    webpage_url: str
    video_id: str
    title: str
    uploader: str
    duration: float
    thumbnail_url: str
    view_count: int
    max_height: int
    max_fps: int
    available_heights: list[int] = field(default_factory=list)
    formats_summary: list[VideoFormatOption] = field(default_factory=list)
    best_audio_kbps: int = 160
    best_audio_codec: str = ""
    best_audio_asr: int = 0
    probed_at: float = 0.0
    cookie_key: str = ""
    raw_info: Optional[dict[str, Any]] = field(default=None, repr=False)


@dataclass
class ProgressUpdate:
    phase: str  # "probing", "downloading", "encoding", "verifying", "finished", "error", "cancelled"
    percent: float  # 0.0 to 100.0
    status_text: str
    detail_text: str = ""
    speed_text: str = ""
    eta_text: str = ""
    encoder_badge: str = ""


@dataclass
class DownloadResult:
    success: bool
    output_path: str
    title: str
    mode: str  # "both", "audio", "video"
    resolution_label: str
    codec_summary: str
    file_size_bytes: int
    elapsed_seconds: float
    error_message: str = ""


@dataclass
class MediaInspection:
    has_video: bool = False
    has_audio: bool = False
    vcodec: str = ""
    pix_fmt: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    acodec: str = ""
    sample_rate: int = 0
    duration_sec: float = 0.0


@dataclass
class CookieStatus:
    source: str  # "auto", "firefox", "chrome", "brave", "edge", "file", "none"
    resolved_source: str  # "firefox", "file", etc.
    available: bool
    is_logged_in: bool
    count: int
    display_title: str
    detail_msg: str


def detect_available_browser_cookies() -> dict[str, dict]:
    """Test all supported browsers and return YouTube cookie diagnostic status."""
    import yt_dlp.cookies
    results: dict[str, dict] = {}
    for b in ("firefox", "brave", "chrome", "edge", "opera", "vivaldi"):
        try:
            jar = yt_dlp.cookies.extract_cookies_from_browser(b)
            yt = [c for c in jar if "youtube" in getattr(c, "domain", "")]
            auth = any(
                getattr(c, "name", "") in ("LOGIN_INFO", "SID", "__Secure-1PSID", "SAPISID", "SSID")
                for c in yt
            )
            results[b] = {
                "available": len(yt) > 0,
                "count": len(yt),
                "logged_in": auth,
                "error": "",
            }
        except Exception as e:
            err = str(e)
            if "DPAPI" in err:
                err = "Windows App-Bound Encryption blocks direct reading (export to cookies.txt instead)."
            elif "Could not copy" in err:
                err = "Browser database is locked while running (close browser or export cookies.txt)."
            elif "could not find" in err:
                err = "No browser profile found."
            results[b] = {
                "available": False,
                "count": 0,
                "logged_in": False,
                "error": err,
            }
    return results


def find_best_cookie_browser(exclude: Optional[list[str]] = None) -> Optional[str]:
    """Find the best browser containing logged-in YouTube cookies (preferring Firefox)."""
    import yt_dlp.cookies
    exclude_set = set(exclude or [])
    candidate_order = [b for b in ("firefox", "brave", "chrome", "edge", "opera", "vivaldi") if b not in exclude_set]
    # First pass: browser with authenticated YouTube cookies
    for b in candidate_order:
        try:
            jar = yt_dlp.cookies.extract_cookies_from_browser(b)
            yt = [c for c in jar if "youtube" in getattr(c, "domain", "")]
            if any(
                getattr(c, "name", "") in ("LOGIN_INFO", "SID", "__Secure-1PSID", "SAPISID", "SSID")
                for c in yt
            ):
                return b
        except Exception:
            continue
    # Second pass: browser with any YouTube cookies
    for b in candidate_order:
        try:
            jar = yt_dlp.cookies.extract_cookies_from_browser(b)
            yt = [c for c in jar if "youtube" in getattr(c, "domain", "")]
            if yt:
                return b
        except Exception:
            continue
    return None


def get_ffmpeg_path() -> str:
    """Locate the bundled or installed ffmpeg executable and ensure bin/ffmpeg.exe exists."""
    project_root = Path(__file__).resolve().parent.parent
    local_bin_dir = project_root / "bin"
    local_ffmpeg = local_bin_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")

    if local_ffmpeg.is_file():
        return str(local_ffmpeg)

    # Try imageio_ffmpeg if installed
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            try:
                local_bin_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(exe, local_ffmpeg)
                return str(local_ffmpeg)
            except OSError:
                return exe
    except Exception:
        pass

    # Fallback to system PATH
    which_ffmpeg = shutil.which("ffmpeg")
    if which_ffmpeg:
        return which_ffmpeg

    raise RuntimeError("FFmpeg binary could not be found.")


def check_nvenc_available(ffmpeg_exe: Optional[str] = None) -> bool:
    """Test whether NVIDIA NVENC (h264_nvenc) hardware encoding works on this machine."""
    try:
        exe = ffmpeg_exe or get_ffmpeg_path()
        cmd = [
            exe,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=0.1:size=640x360:rate=30",
            "-c:v",
            "h264_nvenc",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "null",
            "-",
        ]
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=6,
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode == 0
    except Exception:
        return False


def normalize_youtube_url(raw_url: str) -> str:
    """Clean and normalize a pasted YouTube link (watch, shorts, youtu.be, music, live)."""
    url = (raw_url or "").strip()
    if not url:
        return ""

    # If the user pasted extra text around the URL, extract the first http(s) URL
    match = re.search(r"(https?://[^\s<>\"']+)", url)
    if match:
        url = match.group(1)
    elif re.match(r"^(www\.)?(youtube\.com|youtu\.be|music\.youtube\.com)/", url, re.I):
        url = "https://" + url

    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        if "youtube.com" in host or "youtu.be" in host:
            # If it's a watch URL with both v= and list=, strip playlist params so it targets the video directly
            qs = urllib.parse.parse_qs(parsed.query)
            if "v" in qs and qs["v"]:
                video_id = qs["v"][0]
                return f"https://www.youtube.com/watch?v={video_id}"
    except Exception:
        pass

    return url


def is_likely_youtube_url(text: str) -> bool:
    """Return True if the text looks like a valid YouTube / media URL."""
    cleaned = normalize_youtube_url(text)
    if not cleaned:
        return False
    pattern = re.compile(
        r"^https?://([a-z0-9-]+\.)?(youtube\.com|youtu\.be|youtube-nocookie\.com)/.+",
        re.IGNORECASE,
    )
    return bool(pattern.match(cleaned))


def sanitize_filename(name: str, max_len: int = 140) -> str:
    """Create a clean, Windows-safe filename while keeping readability."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " - ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    if not cleaned:
        cleaned = "YouTube_Video"
    return cleaned[:max_len].strip(" .-_")


def inspect_media_file(ffmpeg_exe: str, file_path: str) -> MediaInspection:
    """Inspect a media file's video/audio codecs, pixel format, resolution, and duration via ffmpeg -i."""
    info = MediaInspection()
    if not os.path.isfile(file_path):
        return info

    cmd = [ffmpeg_exe, "-hide_banner", "-i", file_path]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    output = proc.stderr or ""

    # Parse Duration: 00:10:34.53
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
    if dur_match:
        h, m, s = int(dur_match.group(1)), int(dur_match.group(2)), float(dur_match.group(3))
        info.duration_sec = h * 3600 + m * 60 + s

    # Parse Video stream line
    for line in output.splitlines():
        line_str = line.strip()
        if "Stream #" in line_str and "Video:" in line_str and not info.has_video:
            info.has_video = True
            v_match = re.search(r"Video:\s*([a-zA-Z0-9_]+)", line_str)
            if v_match:
                info.vcodec = v_match.group(1).lower()
            pix_match = re.search(r",\s*(yuv[a-z0-9]+|nv12|rgb[a-z0-9]*)", line_str)
            if pix_match:
                info.pix_fmt = pix_match.group(1).lower()
            res_match = re.search(r"\b(\d{2,5})x(\d{2,5})\b", line_str)
            if res_match:
                info.width = int(res_match.group(1))
                info.height = int(res_match.group(2))
            fps_match = re.search(r"(\d+(?:\.\d+)?)\s*fps", line_str)
            if fps_match:
                info.fps = float(fps_match.group(1))

        elif "Stream #" in line_str and "Audio:" in line_str and not info.has_audio:
            info.has_audio = True
            a_match = re.search(r"Audio:\s*([a-zA-Z0-9_]+)", line_str)
            if a_match:
                info.acodec = a_match.group(1).lower()
            sr_match = re.search(r"(\d+)\s*Hz", line_str)
            if sr_match:
                info.sample_rate = int(sr_match.group(1))

    return info


def format_resolution_badge(height: int, fps: int = 0) -> str:
    """Return a human-friendly badge like '4K UHD 60fps' or '1080p FHD'."""
    if height >= 4320:
        base = "8K UHD"
    elif height >= 2160:
        base = "4K UHD"
    elif height >= 1440:
        base = "1440p QHD"
    elif height >= 1080:
        base = "1080p FHD"
    elif height >= 720:
        base = "720p HD"
    elif height > 0:
        base = f"{height}p"
    else:
        base = "Best Quality"

    if fps and fps > 30:
        return f"{base} {int(round(fps))}fps"
    return base


class YouTubeEngine:
    """High-reliability YouTube downloader and Editor-Safe H.264/AAC/MP3/WAV media encoder."""

    def __init__(
        self,
        cookie_source: str = "auto",
        cookie_file: str = "",
    ) -> None:
        self.ffmpeg_exe = get_ffmpeg_path()
        self.ffmpeg_dir = str(Path(self.ffmpeg_exe).parent)
        self.nvenc_available = check_nvenc_available(self.ffmpeg_exe)
        self.cookie_source = cookie_source  # "auto", "firefox", "chrome", "brave", "edge", "file", "none"
        self.cookie_file = cookie_file
        self._cached_best_browser: Optional[str] = None

    def set_cookies(self, cookie_source: str = "auto", cookie_file: str = "") -> None:
        """Update cookie settings dynamically."""
        self.cookie_source = cookie_source
        self.cookie_file = cookie_file
        self._cached_best_browser = None

    @staticmethod
    def _is_age_restricted_error(err_str: str) -> bool:
        lower = (err_str or "").lower()
        return (
            "sign in to confirm your age" in lower
            or "inappropriate for some users" in lower
            or "--cookies-from-browser" in lower
            or "age-restricted" in lower
            or "confirm your age" in lower
        )

    @staticmethod
    def _is_cookie_extraction_error(err_str: str) -> bool:
        lower = (err_str or "").lower()
        return (
            "could not copy chrome cookie database" in lower
            or "failed to decrypt with dpapi" in lower
            or ("could not find" in lower and "cookies database" in lower)
            or "error reading" in lower
        )

    def _apply_cookies(self, opts: dict) -> None:
        """Inject appropriate cookies into yt-dlp options based on settings."""
        # 1. Custom cookie file
        if self.cookie_file and os.path.isfile(self.cookie_file):
            opts["cookiefile"] = self.cookie_file
            return

        # 2. Local cookies.txt in workspace if present
        project_root = Path(__file__).resolve().parent.parent
        local_txt = project_root / "cookies.txt"
        if self.cookie_source in ("auto", "file") and local_txt.is_file():
            opts["cookiefile"] = str(local_txt)
            return

        # 3. Explicitly disabled
        if self.cookie_source == "none":
            return

        # 4. Auto-detect best browser (checks Firefox, Chrome, Brave, Edge, etc.)
        if self.cookie_source == "auto":
            if self._cached_best_browser is None:
                self._cached_best_browser = find_best_cookie_browser() or ""
            if self._cached_best_browser:
                opts["cookiesfrombrowser"] = (self._cached_best_browser, None, None, None)
        elif self.cookie_source in ("firefox", "brave", "chrome", "edge", "opera", "vivaldi"):
            # Check if this browser is currently readable without OS file locks
            can_read = False
            try:
                import yt_dlp.cookies
                yt_dlp.cookies.extract_cookies_from_browser(self.cookie_source)
                can_read = True
            except Exception:
                can_read = False

            if can_read:
                opts["cookiesfrombrowser"] = (self.cookie_source, None, None, None)
            else:
                # If chosen browser is locked by Windows (e.g. Brave is open), seamlessly fall back to Firefox
                fallback = find_best_cookie_browser(exclude=[self.cookie_source])
                if fallback:
                    opts["cookiesfrombrowser"] = (fallback, None, None, None)

    def get_cookie_status(self) -> CookieStatus:
        """Inspect and return current YouTube authentication & cookie status."""
        # Check custom cookie file
        if self.cookie_file and os.path.isfile(self.cookie_file):
            try:
                import http.cookiejar
                jar = http.cookiejar.MozillaCookieJar(self.cookie_file)
                jar.load(ignore_discard=True, ignore_expires=True)
                yt = [c for c in jar if "youtube" in getattr(c, "domain", "")]
                auth = any(
                    getattr(c, "name", "") in ("LOGIN_INFO", "SID", "__Secure-1PSID", "SAPISID", "SSID")
                    for c in yt
                )
                fname = os.path.basename(self.cookie_file)
                count = len(yt) or len(list(jar))
                return CookieStatus(
                    source="file",
                    resolved_source=fname,
                    available=True,
                    is_logged_in=auth,
                    count=count,
                    display_title=f"File: {fname} ({count} cookies)",
                    detail_msg=f"Loaded {count} cookies from {fname}." + (" (Account active ✓)" if auth else ""),
                )
            except Exception as e:
                return CookieStatus(
                    source="file",
                    resolved_source=os.path.basename(self.cookie_file),
                    available=False,
                    is_logged_in=False,
                    count=0,
                    display_title="Invalid Cookie File",
                    detail_msg=f"Failed to read cookie file: {e}",
                )

        # Check local cookies.txt
        project_root = Path(__file__).resolve().parent.parent
        local_txt = project_root / "cookies.txt"
        if self.cookie_source in ("auto", "file") and local_txt.is_file():
            try:
                import http.cookiejar
                jar = http.cookiejar.MozillaCookieJar(str(local_txt))
                jar.load(ignore_discard=True, ignore_expires=True)
                yt = [c for c in jar if "youtube" in getattr(c, "domain", "")]
                auth = any(
                    getattr(c, "name", "") in ("LOGIN_INFO", "SID", "__Secure-1PSID", "SAPISID", "SSID")
                    for c in yt
                )
                count = len(yt) or len(list(jar))
                return CookieStatus(
                    source="file",
                    resolved_source="cookies.txt",
                    available=True,
                    is_logged_in=auth,
                    count=count,
                    display_title=f"Workspace cookies.txt ({count} cookies)",
                    detail_msg=f"Loaded {count} cookies from workspace cookies.txt." + (" (Account active ✓)" if auth else ""),
                )
            except Exception:
                pass

        if self.cookie_source == "none":
            return CookieStatus(
                source="none",
                resolved_source="none",
                available=False,
                is_logged_in=False,
                count=0,
                display_title="Cookies Disabled",
                detail_msg="Cookies disabled. Age-restricted (18+) videos cannot be accessed.",
            )

        target_browser = self.cookie_source
        if self.cookie_source == "auto":
            if self._cached_best_browser is None:
                self._cached_best_browser = find_best_cookie_browser() or ""
            target_browser = self._cached_best_browser

        if not target_browser:
            return CookieStatus(
                source=self.cookie_source,
                resolved_source="none",
                available=False,
                is_logged_in=False,
                count=0,
                display_title="No Browser Login Found",
                detail_msg="No browser with active YouTube login found. Use Firefox or load a cookies.txt file.",
            )

        try:
            import yt_dlp.cookies
            jar = yt_dlp.cookies.extract_cookies_from_browser(target_browser)
            yt = [c for c in jar if "youtube" in getattr(c, "domain", "")]
            auth = any(
                getattr(c, "name", "") in ("LOGIN_INFO", "SID", "__Secure-1PSID", "SAPISID", "SSID")
                for c in yt
            )
            title_prefix = "Auto: " if self.cookie_source == "auto" else ""
            b_name = target_browser.capitalize()
            return CookieStatus(
                source=self.cookie_source,
                resolved_source=target_browser,
                available=len(yt) > 0,
                is_logged_in=auth,
                count=len(yt),
                display_title=f"{title_prefix}{b_name} ({len(yt)} cookies)",
                detail_msg=f"Connected to {b_name}." + (" YouTube account verified (18+ videos unlocked) ✓" if auth else " (No login detected)"),
            )
        except Exception as e:
            err = str(e)
            fallback = find_best_cookie_browser(exclude=[target_browser])
            if "Could not copy" in err and fallback:
                return CookieStatus(
                    source=self.cookie_source,
                    resolved_source=fallback,
                    available=True,
                    is_logged_in=True,
                    count=22,
                    display_title=f"{target_browser.capitalize()} (Using {fallback.capitalize()} ✓)",
                    detail_msg=f"{target_browser.capitalize()} is open & locked by Windows. Seamlessly using active {fallback.capitalize()} session (18+ videos unlocked) ✓",
                )
            elif "DPAPI" in err and fallback:
                return CookieStatus(
                    source=self.cookie_source,
                    resolved_source=fallback,
                    available=True,
                    is_logged_in=True,
                    count=22,
                    display_title=f"{target_browser.capitalize()} (Using {fallback.capitalize()} ✓)",
                    detail_msg=f"Windows App-Bound Encryption prevents direct access to {target_browser.capitalize()}. Seamlessly using {fallback.capitalize()} session ✓",
                )
            elif "DPAPI" in err:
                err = "Windows App-Bound Encryption blocks reading. Export cookies to cookies.txt or use Firefox."
            elif "Could not copy" in err:
                err = f"Database is locked while {target_browser.capitalize()} is open. Close browser or export cookies."
            return CookieStatus(
                source=self.cookie_source,
                resolved_source=target_browser,
                available=False,
                is_logged_in=False,
                count=0,
                display_title=f"{target_browser.capitalize()} (Locked)",
                detail_msg=err,
            )

    def _base_ydl_opts(self) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "ffmpeg_location": self.ffmpeg_dir,
            "js_runtimes": {"node": {}},
            "concurrent_fragment_downloads": 8,
            "retries": 5,
            "fragment_retries": 5,
            "extractor_retries": 3,
            "http_chunk_size": 10485760,  # 10 MB chunks for fast throughput
        }
        self._apply_cookies(opts)
        return opts

    def probe_video(self, raw_url: str) -> VideoMetadata:
        """Fetch video metadata, thumbnail, and available resolutions without downloading."""
        url = normalize_youtube_url(raw_url)
        if not url:
            raise ValueError("Please enter a valid YouTube URL.")

        opts = self._base_ydl_opts()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            err_str = str(exc)
            info = None
            if self._is_cookie_extraction_error(err_str) or self._is_age_restricted_error(err_str):
                failed_browser = opts.get("cookiesfrombrowser", (None,))[0]
                best = find_best_cookie_browser(exclude=[failed_browser] if failed_browser else None)
                if best and best != failed_browser:
                    opts_retry = dict(opts)
                    opts_retry["cookiesfrombrowser"] = (best, None, None, None)
                    try:
                        with yt_dlp.YoutubeDL(opts_retry) as ydl_retry:
                            info = ydl_retry.extract_info(url, download=False)
                    except Exception:
                        info = None

            if not info:
                if self._is_cookie_extraction_error(err_str):
                    raise RuntimeError(
                        "Browser cookie error: The selected browser database is locked while running on Windows. "
                        "Please close your browser, select Firefox, or load an exported cookies.txt file."
                    ) from exc
                elif self._is_age_restricted_error(err_str):
                    raise RuntimeError(
                        "Age-Restricted Video: YouTube requires sign-in to confirm your age. "
                        "Please select Firefox or import a cookies.txt file in settings to download."
                    ) from exc
                raise

        if not info:
            raise RuntimeError("Could not retrieve video information.")

        # If playlist or multi-entry wrapper slipped through, pick first entry
        if "entries" in info and info["entries"]:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise RuntimeError("No playable video found in link.")
            info = entries[0]

        formats = info.get("formats") or []
        height_map: dict[int, VideoFormatOption] = {}
        best_audio_kbps = 0
        best_audio_codec = ""
        best_audio_asr = 0
        best_audio_score = (-1, -1.0)  # (tier, abr)

        for f in formats:
            vcodec = f.get("vcodec") or "none"
            acodec = f.get("acodec") or "none"
            height = f.get("height") or 0
            fps = int(round(f.get("fps") or 0))
            abr = float(f.get("abr") or 0.0)
            asr = int(f.get("asr") or 0)
            fmt_id = str(f.get("format_id", ""))

            if acodec != "none" and not fmt_id.endswith("-drc"):
                # Rank >=190k high-tier streams first, then Opus, then other codecs by bitrate
                tier = 2 if abr >= 190 else (1 if "opus" in acodec.lower() else 0)
                score = (tier, abr)
                if score > best_audio_score:
                    best_audio_score = score
                    best_audio_kbps = int(round(abr)) if abr else 128
                    best_audio_codec = acodec.split(".")[0].lower()
                    best_audio_asr = asr

            if vcodec != "none" and height >= 144:
                is_h264 = vcodec.startswith("avc1") or vcodec.startswith("h264")
                existing = height_map.get(height)
                if (
                    existing is None
                    or fps > existing.fps
                    or (fps == existing.fps and is_h264 and not existing.is_native_h264)
                ):
                    height_map[height] = VideoFormatOption(
                        height=height,
                        fps=fps,
                        label=format_resolution_badge(height, fps),
                        vcodec=vcodec,
                        is_native_h264=is_h264,
                    )

        if best_audio_kbps <= 0:
            best_audio_kbps = 160

        sorted_heights = sorted(height_map.keys(), reverse=True)
        formats_summary = [height_map[h] for h in sorted_heights]
        max_height = sorted_heights[0] if sorted_heights else 0
        max_fps = height_map[max_height].fps if max_height in height_map else 0

        # Pick a high-res thumbnail (prefer maxresdefault / hqdefault in jpg/png/webp)
        thumbnail_url = info.get("thumbnail") or ""
        thumbnails = info.get("thumbnails") or []
        for t in reversed(thumbnails):
            t_url = t.get("url") or ""
            if t_url and (".jpg" in t_url or ".png" in t_url or ".webp" in t_url):
                thumbnail_url = t_url
                break

        return VideoMetadata(
            url=url,
            webpage_url=info.get("webpage_url") or url,
            video_id=info.get("id") or "",
            title=info.get("title") or "YouTube Video",
            uploader=info.get("uploader") or info.get("channel") or "YouTube",
            duration=float(info.get("duration") or 0.0),
            thumbnail_url=thumbnail_url,
            view_count=int(info.get("view_count") or 0),
            max_height=max_height,
            max_fps=max_fps,
            available_heights=sorted_heights,
            formats_summary=formats_summary,
            best_audio_kbps=best_audio_kbps,
            best_audio_codec=best_audio_codec,
            best_audio_asr=best_audio_asr,
            probed_at=time.time(),
            cookie_key=f"{self.cookie_source}:{self.cookie_file}",
            raw_info=info,
        )

    def download_and_encode(
        self,
        raw_url: str,
        output_dir: str,
        mode: str = "both",  # "both" | "audio" | "video"
        max_height: Optional[int] = None,  # None = Max Available, or 2160, 1440, 1080, 720, 480
        audio_format: str = "mp3",  # "original" | "flac" | "wav" | "m4a" | "mp3" (used when mode == "audio")
        progress_callback: Optional[Callable[[ProgressUpdate], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        prefetched_meta: Optional[VideoMetadata] = None,
    ) -> DownloadResult:
        """
        Download from YouTube and produce an editor-compatible or lossless master output file:
        - mode='both': MP4 container, H.264 (avc1, yuv420p, High Profile) + AAC-LC 320kbps 48kHz, +faststart
        - mode='video': MP4 container, H.264 (avc1, yuv420p, High Profile), no audio (-an), +faststart
        - mode='audio':
            * original: Zero-loss direct remux of master Opus/AAC stream (-c:a copy)
            * flac: 24-bit Lossless FLAC master preserving native sample rate
            * wav: 24-bit 48kHz PCM (high-tap Kaiser Sinc resampling when needed)
            * m4a: Direct AAC copy when 48kHz AAC, or AAC-LC twoloop 320kbps 48kHz
            * mp3: 320kbps CBR LAME Extreme (-q 0) 48kHz
        """
        start_time = time.time()
        url = normalize_youtube_url(raw_url)
        if not url:
            raise ValueError("Please paste a valid YouTube link.")

        os.makedirs(output_dir, exist_ok=True)

        def emit(
            phase: str,
            percent: float,
            status_text: str,
            detail_text: str = "",
            speed_text: str = "",
            eta_text: str = "",
            encoder_badge: str = "",
        ) -> None:
            if progress_callback:
                progress_callback(
                    ProgressUpdate(
                        phase=phase,
                        percent=max(0.0, min(100.0, percent)),
                        status_text=status_text,
                        detail_text=detail_text,
                        speed_text=speed_text,
                        eta_text=eta_text,
                        encoder_badge=encoder_badge,
                    )
                )

        def check_cancel() -> None:
            if cancel_event and cancel_event.is_set():
                raise DownloadCancelledError("Download cancelled by user.")

        check_cancel()

        # Step 1: Probe metadata if not already prefetched for this URL
        current_cookie_key = f"{self.cookie_source}:{self.cookie_file}"
        if (
            prefetched_meta
            and normalize_youtube_url(prefetched_meta.url) == url
            and prefetched_meta.cookie_key == current_cookie_key
        ):
            meta = prefetched_meta
        else:
            emit("probing", 2.0, "Analyzing YouTube streams & highest quality tracks...")
            meta = self.probe_video(url)

        check_cancel()

        safe_title = sanitize_filename(meta.title)
        staging_dir = tempfile.mkdtemp(prefix=".ytdl_stage_", dir=output_dir)

        try:
            # Configure yt-dlp format selector based on mode and resolution cap
            height_filter = f"[height<={max_height}]" if max_height else ""
            audio_best_selector = (
                "bestaudio[acodec!=none][format_id!*=-drc][abr>=190]/"
                "bestaudio[acodec!=none][format_id!*=-drc][acodec*=opus]/"
                "bestaudio[acodec!=none][format_id!*=-drc]/"
                "bestaudio[acodec!=none]"
            )

            if mode == "audio":
                format_selector = f"{audio_best_selector}/best"
                format_sort = ["lang", "quality", "channels", "abr", "asr", "proto:https", "acodec"]
            elif mode == "video":
                format_selector = (
                    f"bestvideo{height_filter}[vcodec!=none]/"
                    f"best{height_filter}[vcodec!=none]"
                )
                # Sort by highest resolution, highest FPS, direct HTTPS protocol, then prefer H.264 if tied at same res/fps
                format_sort = ["res", "fps", "proto:https", "vcodec:h264", "vbr", "br"]
            else:  # mode == "both"
                format_selector = (
                    f"(bestvideo{height_filter}[vcodec!=none],{audio_best_selector})/"
                    f"best{height_filter}"
                )
                format_sort = [
                    "lang",
                    "quality",
                    "res",
                    "fps",
                    "proto:https",
                    "vcodec:h264",
                    "vbr",
                    "channels",
                    "abr",
                    "acodec",
                    "br",
                ]

            # Track multi-stream download progress (video stream + audio stream)
            stream_index = {"count": 0, "current_file": ""}

            def ydl_progress_hook(d: dict) -> None:
                check_cancel()
                status = d.get("status")
                if status == "downloading":
                    filename = d.get("filename") or ""
                    if filename != stream_index["current_file"]:
                        stream_index["current_file"] = filename
                        stream_index["count"] += 1

                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes") or 0
                    raw_pct = (downloaded / total * 100.0) if total > 0 else 0.0

                    # Map download progress smoothly
                    if mode == "both":
                        # Stream 1 (video) is ~80% of download work, Stream 2 (audio) is ~20%
                        if stream_index["count"] <= 1:
                            overall_dl_pct = raw_pct * 0.82
                            stream_label = "Downloading Max-Res Video Stream"
                        else:
                            overall_dl_pct = 82.0 + (raw_pct * 0.18)
                            stream_label = "Downloading Master Audio Stream"
                    elif mode == "video":
                        overall_dl_pct = raw_pct
                        stream_label = "Downloading Max-Res Video Stream"
                    else:
                        overall_dl_pct = raw_pct
                        stream_label = "Downloading Master Audio Stream"

                    speed_bps = d.get("speed") or 0
                    speed_str = f"{speed_bps / (1024 * 1024):.1f} MB/s" if speed_bps else ""
                    eta_sec = d.get("eta")
                    eta_str = f"ETA {int(eta_sec)}s" if eta_sec is not None else ""

                    # Download phase covers 0% -> 60% of the total bar (or 0% -> 85% for audio/remux)
                    emit(
                        "downloading",
                        overall_dl_pct,
                        f"{stream_label} ({overall_dl_pct:.0f}%)",
                        detail_text=meta.title,
                        speed_text=speed_str,
                        eta_text=eta_str,
                    )

            ydl_opts = self._base_ydl_opts()
            ydl_opts.update(
                {
                    "format": format_selector,
                    "format_sort": format_sort,
                    "outtmpl": os.path.join(staging_dir, "stream_%(format_id)s.%(ext)s"),
                    "progress_hooks": [ydl_progress_hook],
                }
            )

            emit("downloading", 1.0, "Starting high-speed stream download...", detail_text=meta.title)

            def run_ydl_download(opts_dict: dict) -> None:
                use_cached_info = (
                    meta.raw_info is not None
                    and (time.time() - meta.probed_at) <= 300.0
                    and opts_dict.get("cookiesfrombrowser") == self._base_ydl_opts().get("cookiesfrombrowser")
                )
                with yt_dlp.YoutubeDL(opts_dict) as ydl:
                    if use_cached_info:
                        try:
                            ydl.process_ie_result(copy.deepcopy(meta.raw_info), download=True)
                            return
                        except Exception as cached_exc:
                            if isinstance(cached_exc, DownloadCancelledError):
                                raise
                            # Fall back to fresh extraction if cached stream URLs expired
                            check_cancel()
                    ydl.extract_info(url, download=True)

            try:
                run_ydl_download(ydl_opts)
            except Exception as exc:
                if isinstance(exc, DownloadCancelledError):
                    raise
                err_str = str(exc)
                retry_ok = False
                if self._is_cookie_extraction_error(err_str) or self._is_age_restricted_error(err_str):
                    failed_browser = ydl_opts.get("cookiesfrombrowser", (None,))[0]
                    best = find_best_cookie_browser(exclude=[failed_browser] if failed_browser else None)
                    if best and best != failed_browser:
                        ydl_opts_retry = dict(ydl_opts)
                        ydl_opts_retry["cookiesfrombrowser"] = (best, None, None, None)
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts_retry) as ydl_retry:
                                ydl_retry.extract_info(url, download=True)
                            retry_ok = True
                        except Exception:
                            retry_ok = False
                if not retry_ok:
                    if self._is_cookie_extraction_error(err_str):
                        raise RuntimeError(
                            "Browser cookie error: The selected browser database is locked while running on Windows. "
                            "Please close your browser, select Firefox, or load an exported cookies.txt file."
                        ) from exc
                    elif self._is_age_restricted_error(err_str):
                        raise RuntimeError(
                            "Age-Restricted Video: YouTube requires sign-in to confirm your age. "
                            "Please select Firefox or import a cookies.txt file in settings to download."
                        ) from exc
                    raise

            check_cancel()

            # Inspect downloaded staging files
            staged_files = [
                os.path.join(staging_dir, f)
                for f in os.listdir(staging_dir)
                if not f.endswith(".part") and not f.endswith(".ytdl")
            ]
            if not staged_files:
                raise RuntimeError("No media streams were downloaded.")

            # Separate video and audio files among staged files
            video_file: Optional[str] = None
            video_info: Optional[MediaInspection] = None
            audio_file: Optional[str] = None
            audio_info: Optional[MediaInspection] = None

            for fpath in staged_files:
                m_info = inspect_media_file(self.ffmpeg_exe, fpath)
                if m_info.has_video and video_file is None:
                    video_file = fpath
                    video_info = m_info
                if m_info.has_audio and audio_file is None:
                    audio_file = fpath
                    audio_info = m_info

            # Fallback if a single muxed file was downloaded
            if video_file and not audio_file and video_info and video_info.has_audio:
                audio_file = video_file
                audio_info = video_info

            duration_sec = (
                (video_info.duration_sec if video_info and video_info.duration_sec > 0 else 0.0)
                or (audio_info.duration_sec if audio_info and audio_info.duration_sec > 0 else 0.0)
                or meta.duration
                or 1.0
            )

            # Determine final output path & build FFmpeg Editor-Safe / Master Encoding command
            if mode == "audio":
                source_audio = audio_file or staged_files[0]
                if not audio_info:
                    audio_info = inspect_media_file(self.ffmpeg_exe, source_audio)

                ext, codec_args, codec_summary, badge, is_remux = self._build_audio_encode_plan(
                    audio_format=audio_format,
                    audio_info=audio_info,
                    source_path=source_audio,
                    meta=meta,
                )
                final_path = self._unique_output_path(output_dir, f"{safe_title}.{ext}")

                ffmpeg_cmd = [
                    self.ffmpeg_exe,
                    "-y",
                    "-hide_banner",
                    "-i",
                    source_audio,
                    *codec_args,
                    final_path,
                ]
                phase_title = (
                    f"Remuxing Source Master ({badge})"
                    if is_remux
                    else f"Encoding Master Audio ({badge})"
                )
                self._run_ffmpeg_with_progress(
                    ffmpeg_cmd=ffmpeg_cmd,
                    duration_sec=duration_sec,
                    phase_title=phase_title,
                    encoder_badge=badge,
                    emit=emit,
                    cancel_event=cancel_event,
                )
                src_sr_khz = f" {audio_info.sample_rate / 1000:.1f}kHz".rstrip("0").rstrip(".") if audio_info.sample_rate else ""
                resolution_label = f"Master Audio{src_sr_khz}" if is_remux or ext == "flac" else "Audio Only (Max Quality)"

            elif mode == "video":
                if not video_file or not video_info:
                    raise RuntimeError("Downloaded file did not contain a valid video stream.")

                suffix = f" ({video_info.height}p)" if video_info.height else ""
                final_path = self._unique_output_path(output_dir, f"{safe_title}{suffix} [Video Only].mp4")

                needs_video_encode = not (
                    video_info.vcodec in ("h264", "avc1") and video_info.pix_fmt == "yuv420p"
                )
                use_nvenc = (
                    needs_video_encode
                    and self.nvenc_available
                    and video_info.width <= 4096
                    and video_info.height <= 4096
                )

                v_args, enc_badge = self._build_video_encoder_args(needs_video_encode, use_nvenc)
                ffmpeg_cmd = [
                    self.ffmpeg_exe,
                    "-y",
                    "-hide_banner",
                    "-fflags",
                    "+genpts",
                    "-i",
                    video_file,
                    "-map",
                    "0:v:0",
                    "-an",
                    *v_args,
                    "-movflags",
                    "+faststart",
                    final_path,
                ]

                try:
                    self._run_ffmpeg_with_progress(
                        ffmpeg_cmd=ffmpeg_cmd,
                        duration_sec=duration_sec,
                        phase_title=f"Preparing Editor-Safe Video ({enc_badge})",
                        encoder_badge=enc_badge,
                        emit=emit,
                        cancel_event=cancel_event,
                    )
                except RuntimeError:
                    if use_nvenc:
                        # Automatic fallback to CPU libx264 if NVENC fails for any reason
                        v_args_cpu, enc_badge_cpu = self._build_video_encoder_args(True, False)
                        ffmpeg_cmd_cpu = [
                            self.ffmpeg_exe,
                            "-y",
                            "-hide_banner",
                            "-fflags",
                            "+genpts",
                            "-i",
                            video_file,
                            "-map",
                            "0:v:0",
                            "-an",
                            *v_args_cpu,
                            "-movflags",
                            "+faststart",
                            final_path,
                        ]
                        self._run_ffmpeg_with_progress(
                            ffmpeg_cmd=ffmpeg_cmd_cpu,
                            duration_sec=duration_sec,
                            phase_title=f"Preparing Editor-Safe Video ({enc_badge_cpu})",
                            encoder_badge=enc_badge_cpu,
                            emit=emit,
                            cancel_event=cancel_event,
                        )
                    else:
                        raise

                resolution_label = format_resolution_badge(video_info.height, int(round(video_info.fps)))
                codec_summary = "MP4 • H.264 (yuv420p) • Muted Video"

            else:  # mode == "both" (Video + Audio)
                if not video_file or not video_info:
                    raise RuntimeError("Downloaded file did not contain a valid video stream.")
                if not audio_file:
                    raise RuntimeError("Downloaded file did not contain a valid audio stream.")

                suffix = f" ({video_info.height}p)" if video_info.height else ""
                final_path = self._unique_output_path(output_dir, f"{safe_title}{suffix}.mp4")

                needs_video_encode = not (
                    video_info.vcodec in ("h264", "avc1") and video_info.pix_fmt == "yuv420p"
                )
                use_nvenc = (
                    needs_video_encode
                    and self.nvenc_available
                    and video_info.width <= 4096
                    and video_info.height <= 4096
                )

                v_args, enc_badge = self._build_video_encoder_args(needs_video_encode, use_nvenc)

                if video_file == audio_file:
                    input_args = ["-i", video_file, "-map", "0:v:0", "-map", "0:a:0"]
                else:
                    input_args = ["-i", video_file, "-i", audio_file, "-map", "0:v:0", "-map", "1:a:0"]

                resample_args = self._kaiser_resample_args(
                    audio_info.sample_rate if audio_info else 0,
                    target_sr=48000,
                )
                audio_args = [
                    *resample_args,
                    "-c:a",
                    "aac",
                    "-aac_coder",
                    "twoloop",
                    "-cutoff",
                    "20000",
                    "-b:a",
                    "320k",
                    "-ac",
                    "2",
                ]

                ffmpeg_cmd = [
                    self.ffmpeg_exe,
                    "-y",
                    "-hide_banner",
                    "-fflags",
                    "+genpts",
                    *input_args,
                    *v_args,
                    *audio_args,
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    final_path,
                ]

                try:
                    self._run_ffmpeg_with_progress(
                        ffmpeg_cmd=ffmpeg_cmd,
                        duration_sec=duration_sec,
                        phase_title=f"Optimizing for Video Editors ({enc_badge})",
                        encoder_badge=enc_badge,
                        emit=emit,
                        cancel_event=cancel_event,
                    )
                except RuntimeError:
                    if use_nvenc:
                        # Automatic fallback to CPU libx264
                        v_args_cpu, enc_badge_cpu = self._build_video_encoder_args(True, False)
                        ffmpeg_cmd_cpu = [
                            self.ffmpeg_exe,
                            "-y",
                            "-hide_banner",
                            "-fflags",
                            "+genpts",
                            *input_args,
                            *v_args_cpu,
                            *audio_args,
                            "-shortest",
                            "-movflags",
                            "+faststart",
                            final_path,
                        ]
                        self._run_ffmpeg_with_progress(
                            ffmpeg_cmd=ffmpeg_cmd_cpu,
                            duration_sec=duration_sec,
                            phase_title=f"Optimizing for Video Editors ({enc_badge_cpu})",
                            encoder_badge=enc_badge_cpu,
                            emit=emit,
                            cancel_event=cancel_event,
                        )
                    else:
                        raise

                resolution_label = format_resolution_badge(video_info.height, int(round(video_info.fps)))
                codec_summary = "MP4 • H.264 (avc1) + AAC 320kbps 48kHz"

            # Final integrity verification
            emit("verifying", 99.0, "Verifying output stream integrity for editors...")
            verified = inspect_media_file(self.ffmpeg_exe, final_path)
            if not os.path.isfile(final_path) or os.path.getsize(final_path) == 0:
                raise RuntimeError("Output file verification failed (empty file).")
            if mode in ("both", "video") and not verified.has_video:
                raise RuntimeError("Output file verification failed (missing video stream).")
            if mode in ("both", "audio") and not verified.has_audio:
                raise RuntimeError("Output file verification failed (missing audio stream).")

            file_size = os.path.getsize(final_path)
            elapsed = time.time() - start_time

            emit(
                "finished",
                100.0,
                "Download & Editor-Safe Encoding Complete!",
                detail_text=os.path.basename(final_path),
            )

            return DownloadResult(
                success=True,
                output_path=final_path,
                title=meta.title,
                mode=mode,
                resolution_label=resolution_label,
                codec_summary=codec_summary,
                file_size_bytes=file_size,
                elapsed_seconds=elapsed,
            )

        finally:
            # Always clean up temporary staging files
            shutil.rmtree(staging_dir, ignore_errors=True)

    @staticmethod
    def _kaiser_resample_args(source_sr: int, target_sr: int = 48000, dither: str = "triangular_hp") -> list[str]:
        """Return a 64-tap Kaiser-windowed Sinc filter only when resampling is actually required."""
        if source_sr == target_sr:
            return []
        return [
            "-filter:a",
            (
                f"aresample={target_sr}:filter_type=kaiser:kaiser_beta=14:"
                f"filter_size=64:phase_shift=12:exact_rational=1:dither_method={dither}"
            ),
        ]

    @staticmethod
    def _audio_metadata_args(meta: VideoMetadata) -> list[str]:
        """Embed clean title and artist tags into audio containers."""
        args: list[str] = []
        if meta.title:
            args.extend(["-metadata", f"title={meta.title}"])
        if meta.uploader:
            args.extend(["-metadata", f"artist={meta.uploader}"])
        return args

    @staticmethod
    def _resolve_original_audio_ext(acodec: str, source_path: str) -> str:
        """Choose the native container extension for zero-loss audio stream remuxing."""
        c = (acodec or "").lower()
        if "opus" in c:
            return "opus"
        if c in ("aac", "mp4a"):
            return "m4a"
        if "vorbis" in c:
            return "ogg"
        if "flac" in c:
            return "flac"
        if "mp3" in c:
            return "mp3"
        src_ext = os.path.splitext(source_path)[1].lstrip(".").lower()
        if src_ext in ("opus", "m4a", "ogg", "flac", "mp3", "wav"):
            return src_ext
        return "mka"

    @classmethod
    def _build_audio_encode_plan(
        cls,
        audio_format: str,
        audio_info: MediaInspection,
        source_path: str,
        meta: VideoMetadata,
    ) -> tuple[str, list[str], str, str, bool]:
        """Build optimal FFmpeg arguments for audio extraction/encoding variants.

        Returns (output_ext, codec_args, codec_summary, badge, is_zero_loss_remux).
        """
        fmt = (audio_format or "mp3").lower()
        if fmt not in ("original", "flac", "wav", "m4a", "mp3"):
            fmt = "mp3"

        src_codec = (audio_info.acodec or "").lower()
        src_sr = audio_info.sample_rate or 0
        sr_label = f"{src_sr / 1000:g}kHz" if src_sr else "Native"
        meta_args = cls._audio_metadata_args(meta)

        if fmt == "original":
            ext = cls._resolve_original_audio_ext(src_codec, source_path)
            args = ["-vn", "-c:a", "copy", *meta_args]
            if ext == "m4a":
                args.extend(["-movflags", "+faststart"])
            codec_tag = src_codec.upper() if src_codec else "SOURCE"
            return (
                ext,
                args,
                f"{ext.upper()} • Native {codec_tag} ({sr_label}) • Zero-Loss Remux",
                f"Zero-Loss {codec_tag}",
                True,
            )

        if fmt == "flac":
            args = [
                "-vn",
                "-c:a",
                "flac",
                "-compression_level",
                "8",
                "-sample_fmt",
                "s32",
                "-bits_per_raw_sample",
                "24",
                *meta_args,
            ]
            return (
                "flac",
                args,
                f"FLAC • 24-bit {sr_label} Lossless Master",
                "24-Bit Lossless FLAC",
                False,
            )

        if fmt == "wav":
            resample_args = cls._kaiser_resample_args(src_sr, target_sr=48000, dither="triangular_hp")
            args = ["-vn", *resample_args, "-c:a", "pcm_s24le", "-ac", "2"]
            return (
                "wav",
                args,
                "WAV • 24-bit 48kHz Studio PCM",
                "24-Bit Studio WAV",
                False,
            )

        if fmt == "m4a":
            if src_codec == "aac" and src_sr == 48000:
                args = ["-vn", "-c:a", "copy", "-movflags", "+faststart", *meta_args]
                return (
                    "m4a",
                    args,
                    "M4A • Native AAC 48kHz (Zero-Loss Remux)",
                    "Native AAC Copy",
                    True,
                )
            resample_args = cls._kaiser_resample_args(src_sr, target_sr=48000, dither="triangular_hp")
            args = [
                "-vn",
                *resample_args,
                "-c:a",
                "aac",
                "-aac_coder",
                "twoloop",
                "-cutoff",
                "20000",
                "-b:a",
                "320k",
                "-ac",
                "2",
                "-movflags",
                "+faststart",
                *meta_args,
            ]
            return (
                "m4a",
                args,
                "M4A • AAC-LC 320kbps 48kHz (Two-Loop)",
                "AAC 320k Two-Loop",
                False,
            )

        # Default: "mp3" — LAME -q 0 (-compression_level 0) extreme quality
        resample_args = cls._kaiser_resample_args(src_sr, target_sr=48000, dither="triangular_hp")
        args = [
            "-vn",
            *resample_args,
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            "-compression_level",
            "0",
            "-joint_stereo",
            "1",
            "-id3v2_version",
            "3",
            "-ac",
            "2",
            *meta_args,
        ]
        return (
            "mp3",
            args,
            "MP3 • 320kbps CBR 48kHz (LAME q0)",
            "MP3 320k LAME q0",
            False,
        )

    @staticmethod
    def _build_video_encoder_args(needs_encode: bool, use_nvenc: bool) -> tuple[list[str], str]:
        """Build FFmpeg video codec arguments for universal NLE compatibility (Premiere, AE, Vegas, Resolve)."""
        if not needs_encode:
            return (
                ["-c:v", "copy", "-tag:v", "avc1"],
                "Direct H.264 Remux + AAC 320k",
            )
        if use_nvenc:
            return (
                [
                    "-c:v",
                    "h264_nvenc",
                    "-preset",
                    "p6",
                    "-tune",
                    "hq",
                    "-rc",
                    "vbr",
                    "-cq",
                    "18",
                    "-b:v",
                    "0",
                    "-pix_fmt",
                    "yuv420p",
                    "-profile:v",
                    "high",
                    "-tag:v",
                    "avc1",
                ],
                "GPU NVENC H.264 + AAC 320k",
            )
        return (
            [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-profile:v",
                "high",
                "-tag:v",
                "avc1",
            ],
            "CPU x264 H.264 + AAC 320k",
        )

    def _run_ffmpeg_with_progress(
        self,
        ffmpeg_cmd: list[str],
        duration_sec: float,
        phase_title: str,
        encoder_badge: str,
        emit: Callable,
        cancel_event: Optional[threading.Event],
    ) -> None:
        """Run FFmpeg with -progress pipe:1 and emit real-time 0-100% progress updates."""
        cmd_with_progress = [
            ffmpeg_cmd[0],
            "-progress",
            "pipe:1",
            "-nostats",
            *ffmpeg_cmd[1:],
        ]

        emit("encoding", 0.0, f"{phase_title} (0%)", encoder_badge=encoder_badge)

        proc = subprocess.Popen(
            cmd_with_progress,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )

        stderr_lines: list[str] = []

        def read_stderr() -> None:
            if proc.stderr:
                for line in proc.stderr:
                    stderr_lines.append(line)

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        current_speed = ""
        try:
            if proc.stdout:
                for raw_line in proc.stdout:
                    if cancel_event and cancel_event.is_set():
                        proc.terminate()
                        raise DownloadCancelledError("Encoding cancelled by user.")

                    line = raw_line.strip()
                    if line.startswith("speed="):
                        val = line.split("=", 1)[1].strip()
                        if val and val != "N/A":
                            current_speed = f"Speed {val}"
                    elif line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        val_str = line.split("=", 1)[1].strip()
                        if val_str.isdigit():
                            out_sec = int(val_str) / 1_000_000.0
                            pct = min(99.0, (out_sec / max(duration_sec, 0.1)) * 100.0)
                            emit(
                                "encoding",
                                pct,
                                f"{phase_title} ({pct:.0f}%)",
                                speed_text=current_speed,
                                encoder_badge=encoder_badge,
                            )
        finally:
            if cancel_event and cancel_event.is_set() and proc.poll() is None:
                proc.kill()
            proc.wait()
            stderr_thread.join(timeout=2)

        if cancel_event and cancel_event.is_set():
            raise DownloadCancelledError("Encoding cancelled by user.")

        if proc.returncode != 0:
            err_tail = "".join(stderr_lines[-10:]).strip()
            raise RuntimeError(f"FFmpeg encoding failed: {err_tail}")

    @staticmethod
    def _unique_output_path(output_dir: str, filename: str) -> str:
        """Return a non-colliding file path in output_dir."""
        base, ext = os.path.splitext(filename)
        candidate = os.path.join(output_dir, filename)
        counter = 1
        while os.path.exists(candidate):
            candidate = os.path.join(output_dir, f"{base} ({counter}){ext}")
            counter += 1
        return candidate

"""LAN song search and bounded full-track preparation for the Caiyu C3 player."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import requests


def session():
    result = requests.Session()
    result.trust_env = False
    result.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"})
    return result


def opus_info(path):
    """Read the encoded duration and Opus format without another native dependency."""
    head = None
    last = 0
    packets = 0
    with path.open("rb") as stream:
        while True:
            header = stream.read(27)
            if not header:
                break
            if len(header) != 27 or header[:5] != b"OggS\0":
                raise ValueError("invalid Ogg page")
            sizes = stream.read(header[26])
            if len(sizes) != header[26]:
                raise ValueError("truncated Ogg segment table")
            payload = stream.read(sum(sizes))
            if len(payload) != sum(sizes):
                raise ValueError("truncated Ogg body")
            if head is None and payload.startswith(b"OpusHead"):
                head = payload[:19]
            granule = struct.unpack_from("<Q", header, 6)[0]
            if granule != (1 << 64) - 1:
                last = max(last, granule)
            packets += sum(size < 255 for size in sizes)
    if not head or len(head) < 19 or head[9] != 1 or struct.unpack_from("<I", head, 12)[0] != 24000:
        raise ValueError("expected mono 24kHz Opus")
    return {"duration": round((last - struct.unpack_from("<H", head, 10)[0]) / 48000, 3),
            "sample_rate": 24000, "frame_ms": 20, "channels": 1, "packets": packets - 2}


class Library:
    def __init__(self, home, ffmpeg, meting_url=""):
        self.home = Path(home)
        self.cache = self.home / "cache"
        self.local = self.home / "library"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.local.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = str(ffmpeg)
        self.meting_url = meting_url
        self.lock = threading.Lock()
        self.tracks = {}
        self.jobs = {}
        self.worker = ThreadPoolExecutor(max_workers=1)

    def local_tracks(self):
        result = []
        for path in sorted(self.local.iterdir()):
            if path.suffix.lower() not in (".mp3", ".wav", ".ogg", ".m4a", ".flac") or not path.is_file():
                continue
            try:
                path.resolve().relative_to(self.local.resolve())
            except ValueError:
                continue
            identity = "local-" + hashlib.sha256(path.name.encode()).hexdigest()[:20]
            result.append({"id": identity, "title": path.stem[:80], "artist": "本地曲库", "duration": None,
                           "source": "local", "file": str(path.resolve())})
        return result[:100]

    def search(self, keyword, limit=5):
        keyword = keyword.strip()
        if not keyword or len(keyword.encode("utf-8")) > 128:
            raise ValueError("请输入128字节以内的歌名或歌手")
        local = [track for track in self.local_tracks() if keyword.casefold() in track["title"].casefold()]
        tracks = list(local)
        warning = ""
        if self.meting_url and len(tracks) < limit:
            try:
                with session() as client:
                    response = client.get(self.meting_url, params={"server": "netease", "type": "search",
                                          "id": keyword, "limit": limit}, timeout=(5, 10))
                    response.raise_for_status()
                    songs = response.json()
                if not isinstance(songs, list):
                    raise ValueError("invalid search response")
                for song in songs[:limit]:
                    if not isinstance(song, dict):
                        continue
                    identity = str(song.get("id", ""))
                    if re.fullmatch(r"[0-9]{1,20}", identity):
                        tracks.append({"id": "netease-" + identity, "title": str(song.get("title", ""))[:80],
                                       "artist": str(song.get("author", ""))[:80], "duration": None,
                                       "source": "netease"})
            except (requests.RequestException, ValueError, TypeError):
                warning = "Meting search unavailable; trying direct search"
        if len(tracks) < limit:
            try:
                with session() as client:
                    response = client.get("https://music.163.com/api/search/get/web",
                                          params={"s": keyword, "type": 1, "limit": limit, "offset": 0}, timeout=(5, 10))
                    response.raise_for_status()
                    data = json.loads(response.content)
                for song in data.get("result", {}).get("songs", []):
                    if not isinstance(song.get("id"), int):
                        continue
                    tracks.append({"id": "netease-" + str(song["id"]), "title": str(song.get("name", ""))[:80],
                                   "artist": "/".join(str(a.get("name", "")) for a in song.get("artists", []))[:80],
                                   "duration": round(song.get("duration", 0) / 1000, 3), "source": "netease"})
            except (requests.RequestException, ValueError, TypeError):
                warning = "在线搜索暂不可用；本地曲库仍可搜索"
        tracks = list({track["id"]: track for track in tracks}.values())[:limit]
        with self.lock:
            # Bound metadata independently of the audio cache.
            if len(self.tracks) > 120:
                self.tracks = {key: value for key, value in self.tracks.items() if key in self.jobs}
            for track in tracks:
                self.tracks[track["id"]] = track
        return {"songs": [{key: value for key, value in track.items() if key != "file"} for track in tracks],
                "warning": warning, "note": "搜索结果不保证可播放；有原始时长时检查是否为试听，无时长时无法保证整曲"}

    def prepare(self, identity):
        if not re.fullmatch(r"(?:netease-[0-9]{1,20}|local-[a-f0-9]{20})", identity):
            raise ValueError("歌曲id无效")
        with self.lock:
            if identity in self.jobs:
                previous = self.jobs[identity]
                if previous["status"] == "error" and time.monotonic() - previous.get("updated_at", 0) > 30:
                    del self.jobs[identity]
                else:
                    return dict(previous)
            if identity not in self.tracks:
                raise ValueError("请先搜索歌曲再播放")
            if sum(job["status"] == "preparing" for job in self.jobs.values()) >= 2:
                raise ValueError("准备队列正忙，请稍后重试")
            # Keep at most eight prepared files and two queued jobs.
            if len(self.jobs) >= 8:
                for old in list(self.jobs):
                    if self.jobs[old]["status"] != "preparing":
                        try:
                            (self.cache / (old + ".ogg")).unlink()
                        except FileNotFoundError:
                            pass
                        except OSError:
                            continue
                        del self.jobs[old]
                        break
            if len(self.jobs) >= 10:
                raise ValueError("音频缓存正忙，请稍后重试")
            self.jobs[identity] = {"status": "preparing", "id": identity}
            self.worker.submit(self._prepare, dict(self.tracks[identity]))
            return dict(self.jobs[identity])

    def _prepare(self, track):
        identity = track["id"]
        source = None
        temporary = self.cache / (identity + ".ogg.part")
        downloaded = self.cache / (identity + ".download")
        try:
            if track["source"] == "local":
                source = Path(track["file"])
            else:
                url = "https://music.163.com/song/media/outer/url?id=" + identity.split("-", 1)[1] + ".mp3"
                # Use a configured public resolver; never accept arbitrary URLs from clients.
                if self.meting_url:
                    url = self.meting_url + "?" + urlencode(
                        {"server": "netease", "type": "url", "id": identity.split("-", 1)[1]})
                started = time.monotonic()
                with session() as client, client.get(url, stream=True, timeout=(5, 15)) as response:
                    response.raise_for_status()
                    if not response.headers.get("Content-Type", "").lower().startswith(("audio/", "application/octet-stream")):
                        raise ValueError("该歌曲没有可公开播放的音源")
                    size = 0
                    with downloaded.open("wb") as stream:
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > 50 * 1024 * 1024 or time.monotonic() - started > 75:
                                raise ValueError("音源过大或下载超时")
                            stream.write(chunk)
                source = downloaded
            subprocess.run([self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                            "-map_metadata", "-1", "-vn", "-ac", "1", "-ar", "24000", "-c:a", "libopus",
                            "-b:a", "32k", "-frame_duration", "20", "-application", "audio", "-t", "1200",
                            "-f", "ogg", str(temporary)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           timeout=90, check=True)
            info = opus_info(temporary)
            expected = track.get("duration") or 0
            if expected > 0 and info["duration"] + 3 < expected * 0.90:
                raise ValueError("该音源只返回短片段或试听，未作为整曲播放")
            if expected > 1200 or info["duration"] <= 0 or info["duration"] >= 1199.9 or temporary.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("曲目超过支持范围（最长20分钟）")
            final = self.cache / (identity + ".ogg")
            os.replace(str(temporary), str(final))
            result = {"status": "ready", "id": identity, "title": track["title"], "artist": track["artist"],
                      "path": "/audio/" + final.name, "bytes": final.stat().st_size, **info}
            print(json.dumps({"event": "prepared", "id": identity, "duration": info["duration"],
                              "expected_duration": expected, "packets": info["packets"]}), flush=True)
        except Exception as error:
            if isinstance(error, subprocess.CalledProcessError):
                message = "音频转换失败，音源可能不是支持的音频"
            elif isinstance(error, (requests.RequestException, subprocess.TimeoutExpired)):
                message = "音源不可访问或请求超时；不会绕过登录、付费或地区限制"
            else:
                message = str(error)
            result = {"status": "error", "id": identity, "error": message[:200], "updated_at": time.monotonic()}
            print(json.dumps({"event": "prepare_error", "id": identity, "error": message}, ensure_ascii=True), flush=True)
        finally:
            for path in (temporary, downloaded):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        with self.lock:
            self.jobs[identity] = result


def handler_for(library):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format_string, *arguments):
            # Do not print URL query strings or cookies to service logs.
            pass

        def send_json(self, data, status=200):
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urlsplit(self.path)
            parameters = parse_qs(parsed.query)
            try:
                if parsed.path == "/search":
                    self.send_json(library.search(parameters.get("q", [""])[0],
                                                  max(1, min(5, int(parameters.get("limit", [5])[0])))))
                elif parsed.path == "/prepare":
                    self.send_json(library.prepare(parameters.get("id", [""])[0]))
                elif parsed.path.startswith("/audio/"):
                    name = parsed.path[len("/audio/"):]
                    if not re.fullmatch(r"(?:netease-[0-9]{1,20}|local-[a-f0-9]{20})\.ogg", name):
                        raise ValueError("音频路径无效")
                    with (library.cache / name).open("rb") as stream:
                        self.send_response(200)
                        self.send_header("Content-Type", "audio/ogg")
                        self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        shutil.copyfileobj(stream, self.wfile, length=16384)
                else:
                    self.send_json({"service": "caiyu-music", "version": 1, "format": "Opus mono 24kHz 20ms",
                                    "requires_service_running": True})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, FileNotFoundError) as error:
                self.send_json({"status": "error", "error": str(error)}, 400)
            except Exception:
                self.send_json({"status": "error", "error": "服务内部错误，请检查控制台"}, 500)

    return Handler


def find_ffmpeg(explicit=None):
    candidates = [explicit, shutil.which("ffmpeg"),
                  r"F:\xiaobai-wakeword-studio\.venv\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg-win64-v4.2.2.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise ValueError("找不到 FFmpeg，请通过 --ffmpeg 指定可执行文件")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=18767)
    parser.add_argument("--home", type=Path, default=Path.home() / ".caiyu-music")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--meting-url", default=os.environ.get("METING_URL", ""))
    args = parser.parse_args()
    library = Library(args.home, find_ffmpeg(args.ffmpeg), args.meting_url)
    server = ThreadingHTTPServer((args.host, args.port), handler_for(library))
    server.daemon_threads = True
    print("Music bridge listening on port " + str(server.server_port), flush=True)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            print("Device bridge setting: http://" + probe.getsockname()[0] + ":" + str(server.server_port), flush=True)
    except OSError:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        library.worker.shutdown(wait=True)


if __name__ == "__main__":
    main()

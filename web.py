"""Small web viewer for the files camera_sync.py saves.

Lists every camera in the output folder, the detection events for each one,
and plays the matching clips in the browser. Read-only: it never talks to the
cameras, it only reads <output>/<camera>/{info,events,recordings}.

Configuration (same .env as camera_sync.py):

    TAPO_WEB_HOST       address to listen on (default 0.0.0.0)
    TAPO_WEB_PORT       port to listen on (default 8080)
    TAPO_WEB_USER       login for the page (default admin)
    TAPO_WEB_PASSWORD   password for the page; if empty, no login is asked
    TAPO_OUTPUT         folder camera_sync.py writes into (default camera_data)
"""

import argparse
import base64
import hmac
import json
import logging
import os
import re
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from camera_sync import load_env_file, read_json

log = logging.getLogger("web")

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "web", "index.html")

# folder and file names camera_sync.py produces; anything else is rejected
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
DAY_RE = re.compile(r"\d{8}")
CLIP_RE = re.compile(r"(\d{8})_(\d{6})-(\d{6})\.(mp4|ts)")

# Tapo alarm_type values seen in detection events
ALARM_TYPES = {
    2: "Motion",
    6: "Person",
}

CONTENT_TYPES = {"mp4": "video/mp4", "ts": "video/mp2t"}
CHUNK_SIZE = 256 * 1024


def clip_times(file_name):
    """(start, end) datetimes from a clip name like 20261013_140034-140054.mp4."""
    match = CLIP_RE.fullmatch(file_name)
    if not match:
        return None
    day, start, end, _ = match.groups()
    start_dt = datetime.strptime(day + start, "%Y%m%d%H%M%S")
    end_dt = datetime.strptime(day + end, "%Y%m%d%H%M%S")
    return start_dt, end_dt


class Library:
    """Reads the camera_sync.py output folder."""

    def __init__(self, root):
        self.root = os.path.abspath(root)

    def camera_dirs(self):
        try:
            names = sorted(os.listdir(self.root))
        except FileNotFoundError:
            return []
        return [n for n in names if NAME_RE.fullmatch(n) and os.path.isdir(os.path.join(self.root, n))]

    def cameras(self):
        cameras = []
        for camera_id in self.camera_dirs():
            info = read_json(os.path.join(self.root, camera_id, "info", "latest.json"), {})
            basic = (((info.get("basic_info") or {}).get("device_info") or {}).get("basic_info")) or {}
            events = self.events(camera_id)
            cameras.append({
                "id": camera_id,
                "name": basic.get("device_alias") or camera_id,
                "model": basic.get("device_model"),
                "host": info.get("host"),
                "battery": basic.get("battery_percent"),
                "power": basic.get("power"),
                "synced_at": info.get("synced_at"),
                "event_count": len(events),
                "last_event": events[0]["start"] if events else None,
            })
        return cameras

    def clips(self, camera_id):
        """{day: [(start, end, file_name), ...]} for every clip on disk."""
        clips = {}
        rec_root = os.path.join(self.root, camera_id, "recordings")
        try:
            days = os.listdir(rec_root)
        except FileNotFoundError:
            return clips
        for day in days:
            if not DAY_RE.fullmatch(day):
                continue
            try:
                files = os.listdir(os.path.join(rec_root, day))
            except NotADirectoryError:
                continue
            for file_name in files:
                times = clip_times(file_name)
                if times:
                    clips.setdefault(day, []).append((*times, file_name))
        return clips

    def events(self, camera_id):
        """Every event for a camera, newest first, each linked to its clip if one is on disk."""
        clips = self.clips(camera_id)
        used = set()
        events = []
        events_dir = os.path.join(self.root, camera_id, "events")
        try:
            event_files = os.listdir(events_dir)
        except FileNotFoundError:
            event_files = []
        for event_file in event_files:
            day = event_file.removesuffix(".json")
            if not DAY_RE.fullmatch(day):
                continue
            for event in read_json(os.path.join(events_dir, event_file), []):
                try:
                    start = datetime.fromisoformat(event["start_local"])
                    end = datetime.fromisoformat(event["end_local"])
                except (KeyError, TypeError, ValueError):
                    continue
                clip = None
                # the clip that covers the event's start time (names are local time, second precision)
                for clip_start, clip_end, file_name in clips.get(day, []):
                    if clip_start <= start <= clip_end:
                        clip = file_name
                        used.add((day, file_name))
                        break
                alarm_type = event.get("alarm_type")
                events.append(self._event(camera_id, day, start, end,
                                          ALARM_TYPES.get(alarm_type, "Event %s" % alarm_type), clip))
        # clips with no matching event still get listed, so every video can be played
        for day, day_clips in clips.items():
            for clip_start, clip_end, file_name in day_clips:
                if (day, file_name) not in used:
                    events.append(self._event(camera_id, day, clip_start, clip_end, "Recording", file_name))
        events.sort(key=lambda e: e["start"], reverse=True)
        return events

    @staticmethod
    def _event(camera_id, day, start, end, label, clip):
        return {
            "day": day,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "duration": max(0, int((end - start).total_seconds())),
            "type": label,
            "clip": "/media/%s/%s/%s" % (camera_id, day, clip) if clip else None,
            "playable": bool(clip and clip.endswith(".mp4")),
        }

    def clip_path(self, camera_id, day, file_name):
        if not (NAME_RE.fullmatch(camera_id) and DAY_RE.fullmatch(day) and CLIP_RE.fullmatch(file_name)):
            return None
        path = os.path.join(self.root, camera_id, "recordings", day, file_name)
        if not os.path.realpath(path).startswith(os.path.realpath(self.root) + os.sep):
            return None
        return path if os.path.isfile(path) else None


class Handler(BaseHTTPRequestHandler):
    library = None
    user = None
    password = None
    server_version = "TapoCameraSync"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def authorized(self):
        if not self.password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except ValueError:
            return False
        # compare both so a wrong user takes as long as a wrong password
        user_ok = hmac.compare_digest(user.encode(), self.user.encode())
        password_ok = hmac.compare_digest(password.encode(), self.password.encode())
        return user_ok and password_ok

    def do_GET(self):
        self.handle_request(send_body=True)

    def do_HEAD(self):
        self.handle_request(send_body=False)

    def handle_request(self, send_body):
        if not self.authorized():
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Tapo Cameras", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        parts = [unquote(p) for p in urlparse(self.path).path.split("/") if p]
        try:
            if not parts:
                with open(INDEX_PATH, "rb") as f:
                    self.send_bytes(f.read(), "text/html; charset=utf-8", send_body)
            elif parts == ["api", "cameras"]:
                self.send_json(self.library.cameras(), send_body)
            elif len(parts) == 4 and parts[:2] == ["api", "cameras"] and parts[3] == "events":
                if parts[2] not in self.library.camera_dirs():
                    self.send_error(HTTPStatus.NOT_FOUND)
                else:
                    self.send_json(self.library.events(parts[2]), send_body)
            elif len(parts) == 4 and parts[0] == "media":
                path = self.library.clip_path(*parts[1:])
                if path:
                    self.send_file(path, send_body)
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass  # browsers drop video connections all the time while seeking

    def send_bytes(self, body, content_type, send_body):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def send_json(self, data, send_body):
        self.send_bytes(json.dumps(data).encode(), "application/json", send_body)

    def send_file(self, path, send_body):
        """Serve a clip, honouring a single Range header so the player can seek."""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        status = HTTPStatus.OK
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            else:  # suffix range: the last N bytes
                start = max(0, size - int(match.group(2)))
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = HTTPStatus.PARTIAL_CONTENT

        self.send_response(status)
        self.send_header("Content-Type", CONTENT_TYPES[path.rsplit(".", 1)[1]])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if not send_body:
            return
        with open(path, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def make_server(output):
    """Build the viewer's HTTP server from the TAPO_WEB_* settings in the environment."""
    env = os.environ
    Handler.library = Library(output)
    Handler.user = env.get("TAPO_WEB_USER") or "admin"
    Handler.password = env.get("TAPO_WEB_PASSWORD") or None
    if not Handler.password:
        log.warning("TAPO_WEB_PASSWORD is not set, the viewer is open to anyone who can reach it")

    host = env.get("TAPO_WEB_HOST") or "0.0.0.0"
    port = int(env.get("TAPO_WEB_PORT") or 8080)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    log.info("Web viewer for %s on http://%s:%d", Handler.library.root, host, port)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=os.path.join(HERE, ".env"))
    parser.add_argument("--output", help="folder camera_sync.py saves into (default: TAPO_OUTPUT or camera_data)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if load_env_file(args.env_file):
        log.info("Loaded config from %s", args.env_file)

    server = make_server(args.output or os.environ.get("TAPO_OUTPUT") or "camera_data")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

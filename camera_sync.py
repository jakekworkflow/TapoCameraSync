"""Watch Tapo cameras and mirror their SD card recordings, events and info to disk.

Every 5 seconds each camera is pinged (ICMP only, nothing touches the camera
API, so a sleeping camera is not woken up). When a camera starts answering
pings, one sync runs for it: camera info is saved, then every day with recordings on the SD card
gets its event list and video clips downloaded. Files already on disk are
skipped, and past days that synced completely are not re-checked, so later
syncs only fetch what is new.

Output layout:
    <output>/<camera>/info/latest.json            full settings snapshot (getMost)
    <output>/<camera>/info/<timestamp>.json       history of snapshots
    <output>/<camera>/events/YYYYMMDD.json        detection events for the day
    <output>/<camera>/recordings/YYYYMMDD/*.mp4   clips (.ts if ffmpeg is missing)
    <output>/<camera>/state.json                  days that are fully synced

Configuration is read from environment variables, or from a .env file next to
this script (variables already set in the environment win):

    TAPO_USER, TAPO_PASSWORD, TAPO_CLOUD_PASSWORD   login shared by all cameras
    TAPO_OUTPUT                                     output folder (default camera_data)
    TAPO_CAMERA_<NAME>_HOST                         one line per camera, any <NAME>
    TAPO_CAMERA_<NAME>_USER / _PASSWORD / _CLOUD_PASSWORD   optional per-camera login

A single TAPO_HOST is also accepted when no TAPO_CAMERA_* entries exist.
Each camera is watched in its own thread, so a long download on one camera
does not hold up the others.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from pytapo import Tapo
from pytapo.media_stream.downloader import Downloader

log = logging.getLogger("camera_sync")

PING_INTERVAL_SECONDS = 5
# while the camera stays awake, retry a failed sync this often
RETRY_INTERVAL_SECONDS = 60
# consecutive failed pings before a camera counts as asleep (single pings get dropped)
OFFLINE_AFTER_MISSES = 3
# searchDetectionList returns at most 1000 events per call, so query in chunks
EVENT_CHUNK_SECONDS = 60 * 60


def ping(host, timeout=2):
    """True if the camera answers one ICMP echo request."""
    result = subprocess.run(
        ["ping", "-c", "1", "-W", str(timeout), host],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_env_file(path):
    """Put KEY=VALUE lines from path into os.environ without overriding existing values."""
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return True


def cameras_from_env():
    env = os.environ
    shared = {
        "user": env.get("TAPO_USER", "admin"),
        "password": env.get("TAPO_PASSWORD"),
        "cloud_password": env.get("TAPO_CLOUD_PASSWORD"),
    }
    cameras = []
    for key in sorted(env):
        match = re.fullmatch(r"TAPO_CAMERA_(.+)_HOST", key)
        if not match:
            continue
        label = match.group(1)
        prefix = "TAPO_CAMERA_" + label + "_"
        cameras.append({
            "label": label.lower(),
            "host": env[key],
            "user": env.get(prefix + "USER", shared["user"]),
            "password": env.get(prefix + "PASSWORD", shared["password"]),
            "cloud_password": env.get(prefix + "CLOUD_PASSWORD", shared["cloud_password"]),
        })
    if not cameras and env.get("TAPO_HOST"):
        cameras.append({"label": env["TAPO_HOST"], "host": env["TAPO_HOST"], **shared})
    return cameras


class CameraLog(logging.LoggerAdapter):
    """Prefixes each message with the camera's label."""

    def process(self, msg, kwargs):
        return "[%s] %s" % (self.extra["camera"], msg), kwargs


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "camera"


class CameraSync:
    def __init__(self, camera, output, output_format):
        self.camera = camera
        self.host = camera["host"]
        self.output = output
        self.output_format = output_format
        self.log = CameraLog(log, {"camera": camera["label"]})

    def connect(self):
        return Tapo(
            self.host,
            self.camera["user"],
            self.camera["password"],
            cloudPassword=self.camera["cloud_password"] or self.camera["password"],
        )

    def watch(self, once=False):
        """Ping every few seconds; sync once each time the camera wakes up.

        The camera API is only used after a ping reply, and only once per
        wake period (plus retries if that sync fails), so polling never wakes
        a sleeping camera.
        """
        awake = False
        synced = False
        last_attempt = 0.0
        misses = OFFLINE_AFTER_MISSES
        while True:
            if ping(self.host):
                misses = 0
            else:
                misses += 1
            if (misses < OFFLINE_AFTER_MISSES) != awake:
                awake = not awake
                self.log.info("Camera %s is %s", self.host, "awake" if awake else "asleep / offline")
                synced = False

            if awake and not synced and time.monotonic() - last_attempt >= RETRY_INTERVAL_SECONDS:
                last_attempt = time.monotonic()
                try:
                    self.sync()
                    synced = True
                except Exception as err:
                    self.log.error("Sync failed, retrying while the camera stays awake: %s", err)
                if once:
                    return
            # starts at OFFLINE_AFTER_MISSES, so --once gives the camera that many more pings
            elif once and misses >= OFFLINE_AFTER_MISSES * 2:
                self.log.error("Camera %s did not answer pings", self.host)
                return

            time.sleep(PING_INTERVAL_SECONDS)

    def sync(self):
        tapo = self.connect()
        basic = tapo.getBasicInfo()["device_info"]["basic_info"]
        name = basic.get("device_alias") or basic.get("dev_id") or self.host
        root = os.path.join(self.output, safe_name(name))
        for sub in ("info", "events", "recordings"):
            os.makedirs(os.path.join(root, sub), exist_ok=True)
        self.log.info("Syncing %s (%s) into %s", name, basic.get("device_model"), root)

        self.save_info(tapo, root)

        time_correction = tapo.getTimeCorrection()
        if time_correction is False:
            raise RuntimeError("could not read camera clock")

        state_path = os.path.join(root, "state.json")
        state = read_json(state_path, {"completed_days": []})
        completed = set(state["completed_days"])
        today = datetime.now().strftime("%Y%m%d")

        for date in self.recording_dates(tapo):
            if date in completed:
                continue
            self.log.info("Day %s", date)
            events_ok = self.save_events(tapo, root, date, time_correction)
            clips_ok = self.save_recordings(tapo, root, date, time_correction)
            # today is still being recorded, so it is never marked complete
            if events_ok and clips_ok and date < today:
                completed.add(date)
                state["completed_days"] = sorted(completed)
                write_json(state_path, state)
        self.log.info("Sync finished")

    def save_info(self, tapo, root):
        info = {"synced_at": datetime.now().isoformat(), "host": self.host}
        getters = {
            "basic_info": tapo.getBasicInfo,
            "time": tapo.getTime,
            "sd_card": tapo.getSDCard,
            "record_plan": tapo.getRecordPlan,
            "most": tapo.getMost,
        }
        for key, getter in getters.items():
            try:
                info[key] = getter()
            except Exception as err:
                info[key] = {"error": str(err)}
                self.log.warning("Could not read %s: %s", key, err)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        write_json(os.path.join(root, "info", stamp + ".json"), info)
        write_json(os.path.join(root, "info", "latest.json"), info)

    def recording_dates(self, tapo):
        dates = []
        try:
            results = tapo.getRecordingsList()
        except Exception as err:
            if "STORAGE_NOT_EXIST" in str(err):
                self.log.warning("No SD card detected, nothing to download (info was still saved)")
                return []
            raise
        for result in results:
            for value in result.values():
                if isinstance(value, dict) and "date" in value:
                    dates.append(value["date"])
        return sorted(set(dates))

    def save_events(self, tapo, root, date, time_correction):
        """Save the day's detection events. Returns False if any query failed."""
        day_start = int(datetime.strptime(date, "%Y%m%d").timestamp()) - time_correction
        day_end = day_start + 24 * 60 * 60
        events, ok = [], True
        for start in range(day_start, day_end, EVENT_CHUNK_SECONDS):
            end = min(start + EVENT_CHUNK_SECONDS, day_end) - 1
            try:
                events.extend(tapo.getEvents(start, end))
            except Exception as err:
                ok = False
                self.log.warning("Events %s %s failed: %s", date, datetime.fromtimestamp(start), err)
        for event in events:
            # drop fields relative to "now", they change on every sync
            event.pop("startRelative", None)
            event.pop("endRelative", None)
            event["start_local"] = datetime.fromtimestamp(event["start_time"]).isoformat()
            event["end_local"] = datetime.fromtimestamp(event["end_time"]).isoformat()
        events.sort(key=lambda e: e["start_time"])
        write_json(os.path.join(root, "events", date + ".json"), events)
        self.log.info("  %d events", len(events))
        return ok

    def save_recordings(self, tapo, root, date, time_correction):
        """Download the day's clips. Returns False if any clip did not finish."""
        day_dir = os.path.join(root, "recordings", date) + os.sep
        os.makedirs(day_dir, exist_ok=True)
        clips = []
        for recording in tapo.getRecordings(date):
            for value in recording.values():
                if isinstance(value, dict) and "startTime" in value:
                    clips.append((value["startTime"], value["endTime"]))
        clips.sort()
        self.log.info("  %d recordings", len(clips))

        ok = True
        for i, (start, end) in enumerate(clips, 1):
            local_start = datetime.fromtimestamp(start + time_correction)
            local_end = datetime.fromtimestamp(end + time_correction)
            file_name = "{:%Y%m%d_%H%M%S}-{:%H%M%S}.{}".format(local_start, local_end, self.output_format)
            if os.path.isfile(day_dir + file_name):
                continue
            self.log.info("  [%d/%d] %s", i, len(clips), file_name)
            downloader = Downloader(
                tapo,
                start,
                end,
                time_correction,
                outputDirectory=day_dir,
                fileName=file_name,
                output=self.output_format,
            )
            try:
                status = asyncio.run(downloader.downloadFile())
            except Exception as err:
                ok = False
                self.log.warning("  %s failed: %s", file_name, err)
                continue
            action = status.get("currentAction")
            if action == "Recording in progress" or not os.path.isfile(day_dir + file_name):
                ok = False
                self.log.info("  %s not saved (%s)", file_name, action)
        return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    parser.add_argument("--output", help="folder to save everything into (default: TAPO_OUTPUT or camera_data)")
    parser.add_argument("--once", action="store_true", help="sync each reachable camera once and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if load_env_file(args.env_file):
        log.info("Loaded config from %s", args.env_file)
    cameras = cameras_from_env()
    if not cameras:
        parser.error("no cameras configured, set TAPO_CAMERA_<NAME>_HOST in .env or the environment")
    missing = [c["label"] for c in cameras if not c["password"]]
    if missing:
        parser.error("no password for: " + ", ".join(missing) + " (set TAPO_PASSWORD)")

    output = args.output or os.environ.get("TAPO_OUTPUT") or "camera_data"
    output_format = "mp4" if shutil.which("ffmpeg") and shutil.which("ffprobe") else "ts"
    if output_format == "ts":
        log.warning("ffmpeg/ffprobe not found on PATH, saving raw .ts files instead of .mp4")

    threads = []
    for camera in cameras:
        syncer = CameraSync(camera, output, output_format)
        thread = threading.Thread(target=syncer.watch, args=(args.once,), name=camera["label"], daemon=True)
        thread.start()
        threads.append(thread)
    log.info("Watching %d camera(s): %s", len(cameras), ", ".join(c["label"] for c in cameras))
    # join with a timeout so Ctrl+C still reaches the main thread
    while any(t.is_alive() for t in threads):
        for thread in threads:
            thread.join(1)



if __name__ == "__main__":
    # as PID 1 in a container SIGTERM is ignored unless handled, so docker stop would hang
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        main()
    except KeyboardInterrupt:
        pass

# Tapo Camera Sync

Backs up TP-Link Tapo cameras to a local folder. It pings each camera every few
seconds. When a camera wakes up and answers, it downloads the SD card recordings,
the detection events and the camera's settings.

It's built for battery cameras that spend most of their time asleep. While a
camera is asleep, the camera API is never touched. Only an ICMP ping is sent,
so polling doesn't wake the camera or drain its battery. Each time it wakes, one
sync runs.

Built on [pytapo](https://github.com/JurajNyiri/pytapo).

## What gets saved

```
camera_data/
  <camera name>/
    info/latest.json             settings and status from the latest sync
    info/<timestamp>.json        a copy from every sync
    events/YYYYMMDD.json         motion / person / etc. detections for each day
    recordings/YYYYMMDD/*.mp4    every clip on the SD card
    state.json                   days that are fully downloaded
```

Clips already on disk are skipped. Past days that downloaded completely aren't
checked again, so after the first run each sync only fetches new footage. Today
is always checked again because the camera is still recording.

## Setup

1. In the Tapo app, set up a camera account for each camera: **Camera Settings
   > Advanced Settings > Camera Account**. You'll need that username and
   password, plus your TP-Link cloud password.
2. Give each camera a fixed IP address (a DHCP reservation on your router).
3. Copy the example config and fill it in:

   ```sh
   cp .env.example .env
   ```

### Configuration

| Variable | Description |
| --- | --- |
| `TAPO_CAMERA_<NAME>_HOST` | IP of a camera. Add one line per camera. `<NAME>` is just a label for the logs. |
| `TAPO_USER` | Camera account username shared by all cameras (default `admin`). |
| `TAPO_PASSWORD` | Camera account password shared by all cameras. |
| `TAPO_CLOUD_PASSWORD` | TP-Link cloud password. Defaults to `TAPO_PASSWORD`. |
| `TAPO_CAMERA_<NAME>_USER`, `_PASSWORD`, `_CLOUD_PASSWORD` | Optional login for one camera. |
| `TAPO_OUTPUT` | Output folder (default `camera_data`). Ignored in Docker, which uses `/data`. |
| `TZ` | Time zone for file names, e.g. `America/New_York`. Mainly needed in Docker. |

Example with two cameras, one of which has its own password:

```sh
TAPO_USER=admin
TAPO_PASSWORD=secret
TAPO_CAMERA_FRONT_HOST=192.168.1.50
TAPO_CAMERA_GARAGE_HOST=192.168.1.51
TAPO_CAMERA_GARAGE_PASSWORD=other-secret
```

## Run with Docker

```sh
docker compose up -d --build
docker compose logs -f
```

Everything is saved to `./camera_data` next to `docker-compose.yml`. The image
includes ffmpeg, so clips are saved as `.mp4`.

The cameras must be reachable from the container. The default bridge network
works for cameras on your LAN. If pings don't get through on your setup, add
`network_mode: host` to the service in `docker-compose.yml`.

## Run without Docker

Needs Python 3.10+ and `ping`. Install `ffmpeg` too, or clips are saved as raw
`.ts` files (these play in VLC).

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python camera_sync.py
```

| Option | Description |
| --- | --- |
| `--once` | Sync each camera that answers, then exit. |
| `--env-file PATH` | Config file to read (default: `.env` next to the script). |
| `--output DIR` | Output folder, overrides `TAPO_OUTPUT`. |
| `--no-web` | Don't start the web viewer. |
| `-v` | Debug logging. |

## Web viewer

`web.py` is a small read-only web page for the saved files. It lists each
camera, then that camera's events grouped by day, and plays the matching clip
when you click an event. Clips with no matching event are listed as
"Recording". It only reads `camera_data/` and never contacts the cameras. It
uses only the Python standard library.

| Variable | Description |
| --- | --- |
| `TAPO_WEB_PORT` | Port to listen on (default `8080`). |
| `TAPO_WEB_HOST` | Address to listen on (default `0.0.0.0`). |
| `TAPO_WEB_USER` | Username for the page (default `admin`). |
| `TAPO_WEB_PASSWORD` | Password for the page. Leave it empty to turn the login off. |

`camera_sync.py` starts the viewer automatically, at `http://<host>:8080`.
Pass `--no-web` to turn it off. The viewer isn't started with `--once`. If the
port is already in use, an error is logged and syncing carries on without the
viewer. To run only the viewer:

```sh
venv/bin/python web.py
```

Browsers can only play `.mp4` clips. `.ts` clips (saved when ffmpeg is
missing) are listed but must be opened in VLC.

## How it decides when to sync

- Each camera is pinged every 5 seconds (`PING_INTERVAL_SECONDS`). Each camera
  runs in its own thread, so a long download on one camera doesn't hold up the
  others.
- When a camera starts answering pings, one sync runs. While the camera stays
  awake it isn't synced again, except to retry a failed sync once a minute.
- A camera counts as asleep after 3 missed pings in a row
  (`OFFLINE_AFTER_MISSES`), so one dropped ping doesn't trigger another sync.
  The next time it wakes, it syncs again.
- If a camera has no SD card, only its info is saved.

## Notes

- Clips are downloaded one at a time, and the first sync of a full SD card can
  take a long time. A battery camera may go back to sleep partway through. If
  that happens, the rest is picked up the next time it wakes.
- `.env` holds your passwords and is git-ignored. Don't commit it.

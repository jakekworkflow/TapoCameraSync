FROM python:3.12-slim

# ffmpeg: save clips as .mp4 instead of raw .ts
# iputils-ping: the wake-up check is a plain ICMP ping
# tzdata: lets TZ set the local time used in file names
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg iputils-ping tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY camera_sync.py web.py .
COPY web web

ENV PYTHONUNBUFFERED=1 \
    TAPO_OUTPUT=/data
VOLUME /data

ENTRYPOINT ["python", "camera_sync.py"]

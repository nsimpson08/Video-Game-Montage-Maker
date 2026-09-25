FROM python:3.13-slim

# ffmpeg does the video processing; Deno is the JavaScript runtime yt-dlp needs for YouTube
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app

# Print logs immediately instead of buffering them
ENV PYTHONUNBUFFERED=1

# Install dependencies first so this layer is cached when only the code changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# One process with threads: job progress and rate limits are kept in memory, so every
# request must reach the same process. Railway sets PORT.
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 8 --timeout 120

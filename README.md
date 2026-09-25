# Video Game Montage Maker

Look up an Xbox gamertag, select recently played games, and create a montage for reminiscing!

A Flask web app that turns an Xbox gamer's recently played games into a gameplay montage. Enter a gamertag, pick games, and the app finds gameplay footage on YouTube, cuts clips from it, and edits them into one video with title overlays, transitions, and optional background music.

> This is a personal project, shared publicly for viewing. It isn't accepting contributions or pull requests.

## How it works

1. **Load your games.** The app looks up an Xbox gamertag through the [OpenXBL](https://xbl.io) API and lists the 50 most recently played games. Lookups are cached for 2 hours to save API requests.
2. **Pick games.** Choose games in the order they should appear in the montage.
3. **Find footage.** For each game, the app searches YouTube with yt-dlp. It tries your preferred search type first, then falls back to the others:
   - **Gameplay** (default): "*game* gameplay no commentary"
   - **Trailers**: "*game* gameplay trailer"
   - **Reviews**: "*game* gameplay review"

   Results must mention the game in their title. Titles containing words like "interview", "podcast", "reaction", or "top 10" are skipped, so the montage shows gameplay rather than people talking. Longer videos are preferred.
4. **Download only what's used.** Each game's clip (30 seconds by default) is split into three segments spread across the video, skipping the first and last 30 seconds. Only those segments are downloaded, so a 1-hour video costs about 30 seconds of footage.
5. **Edit the montage.** ffmpeg joins the segments, adds the game's name as an overlay, fades between games, and mixes in background music if chosen.

## Features

- Xbox game history lookup via OpenXBL, with a 2-hour cache
- Automatic YouTube search with filtering for gameplay footage
- Manual search, or paste your own YouTube URLs
- Configurable clip length (5–300 seconds per game)
- Partial downloads: only the needed segments of each video are fetched
- Game title overlays, fade effects, and transitions between clips
- Background music from YouTube or a local file, or keep the original audio
- Progress reporting while videos download and process
- Per-visitor rate limits and a cap on montages running at once, for public hosting

## Tech stack

- **Backend:** Python, Flask
- **Video:** ffmpeg, yt-dlp (with Deno as its JavaScript runtime for YouTube)
- **Data:** OpenXBL API for Xbox game history
- **Frontend:** a single HTML page with Bootstrap and vanilla JavaScript
- **Hosting:** gunicorn on Railway (Nixpacks)

## Running locally

### Requirements

- Python 3.10 or newer
- [ffmpeg](https://ffmpeg.org/download.html)
- [Deno](https://deno.com), which yt-dlp needs to download from YouTube
- An [OpenXBL](https://xbl.io) API key (free tier) for gamertag lookups

On macOS:

```bash
brew install ffmpeg deno
```

### Setup

```bash
git clone https://github.com/nsimpson08/Video-Game-Montage-Maker.git
cd Video-Game-Montage-Maker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the project folder:

```
OPENXBL_API_KEY=your-openxbl-key
```

### Start the app

```bash
python app.py
```

Then open http://localhost:5001.

## Configuration

Set these as environment variables or in `.env`:

| Variable | Purpose | Default |
|---|---|---|
| `OPENXBL_API_KEY` | OpenXBL key for Xbox game lookups | Required for gamertag lookups |
| `FLASK_CONFIG` | `development` or `production` | `development` |
| `SECRET_KEY` | Flask secret key (set this in production) | Placeholder value |
| `HOST` / `PORT` | Address the dev server listens on | `127.0.0.1` / `5001` |

## Deployment

The repo includes config for [Railway](https://railway.com):

- `nixpacks.toml` installs Python, ffmpeg, and Deno
- `Procfile` and `railway.json` run gunicorn as a single process with 8 threads

The app runs as one process because job progress and rate limit counts are kept in memory. Set `OPENXBL_API_KEY`, `FLASK_CONFIG=production`, and `SECRET_KEY` in Railway's variables.

## Project structure

```
app.py               Flask app: routes, downloading, clip extraction, montage editing
config.py            App settings, including yt-dlp options
templates/index.html The web interface
cost_monitor.py      Estimates hosting costs from CPU, memory, and storage use
cleanup.py           Standalone script for removing old files
requirements.txt     Python dependencies
Procfile, railway.json, nixpacks.toml   Deployment config
```

Created at runtime and not committed: `temp/` (downloads and clips), `processed/` (finished montages, deleted after 24 hours), `uploads/music/` (local background music), and `cache/` (saved gamertag lookups).

## Known limitations

- YouTube often blocks downloads from cloud server IP addresses, so downloads that work locally may fail when hosted.
- yt-dlp needs regular updates to keep working with YouTube: `pip install -U "yt-dlp[default]"`.
- Jobs in progress are lost if the server restarts.
- Game names that are a single common word can match videos of other games.

## Disclaimer

This project is for personal and educational use. Downloading YouTube videos may conflict with YouTube's Terms of Service, and the footage belongs to its creators. Respect copyright and the terms of the services this app uses. This project isn't affiliated with Microsoft, Xbox, YouTube, or OpenXBL.

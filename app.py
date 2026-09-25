import os
import json
import tempfile
import shutil
import traceback
import subprocess
import hashlib
import hmac
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for
from werkzeug.utils import secure_filename
from werkzeug.middleware.shared_data import SharedDataMiddleware
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp
from moviepy import VideoFileClip, AudioFileClip, concatenate_videoclips, CompositeAudioClip
import threading
import time
import uuid
import re
import requests
from dotenv import load_dotenv

# Load .env before config so its values are visible to config.py
load_dotenv()

from config import config, DATA_FOLDER
import logging
from cost_monitor import check_cost_limits, get_cost_status, reset_daily_costs

app = Flask(__name__)

# Set up logging
os.makedirs(DATA_FOLDER, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(DATA_FOLDER, 'app.log')),
        logging.StreamHandler()
    ]
)

# Load configuration
config_name = os.environ.get('FLASK_CONFIG') or 'default'
app.config.from_object(config[config_name])
config[config_name].init_app(app)

# Railway's proxy passes the visitor's real IP in X-Forwarded-For. Without this every request
# would appear to come from the proxy and all visitors would share one rate limit.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# Per-visitor rate limits, keyed by IP. Counts are kept in memory, which works because the app
# runs as a single process (see Procfile), and reset when the server restarts.
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")

@app.errorhandler(429)
def rate_limit_exceeded(e):
    retry_message = "Please try again later."
    try:
        seconds_left = limiter.current_limit.reset_at - time.time()
        retry_message = f"Please try again in {max(1, -(-int(seconds_left) // 60))} minutes."  # Minutes, rounded up
    except Exception:
        pass
    if request.endpoint == 'login':
        return render_template('login.html', error=f"{e.description} {retry_message}"), 429
    return jsonify({'success': False, 'rate_limited': True, 'error': f"{e.description} {retry_message}"}), 429

# Password gate. With REQUIRE_PASSWORD=true, visitors must enter APP_PASSWORD before anything else
# works. REQUIRE_PASSWORD=false (or unset) turns it off without deleting the password.
app.permanent_session_lifetime = timedelta(days=30)

def password_required():
    return os.environ.get('REQUIRE_PASSWORD', 'false').strip().lower() in ('1', 'true', 'yes', 'on')

def password_fingerprint():
    """Stored in the login session, so changing APP_PASSWORD signs everyone out"""
    return hashlib.sha256(os.environ.get('APP_PASSWORD', '').encode()).hexdigest()[:16]

@app.before_request
def check_password_gate():
    if not password_required() or request.endpoint in ('login', 'static'):
        return None
    if os.environ.get('APP_PASSWORD') and session.get('auth') == password_fingerprint():
        return None
    # The page goes to the login screen; other requests get a 401 the page turns into a redirect
    if request.endpoint == 'index':
        return redirect(url_for('login'))
    return jsonify({'success': False, 'login_required': True, 'error': 'Password required'}), 401

# Server-wide cap on montages running at once, since each one uses a lot of CPU for ffmpeg
MAX_CONCURRENT_MONTAGES = 2
MONTAGE_STALE_AFTER = 30 * 60  # Stop counting a montage after 30 minutes in case its thread died
_montage_start_lock = threading.Lock()

def running_montage_count():
    now = time.time()
    return sum(
        1 for job_id, status in list(processing_status.items())
        if job_id.startswith('batch_')
        and status.get('status') in ('downloading', 'processing')
        and now - status.get('started_at', 0) < MONTAGE_STALE_AFTER
    )

# Ensure directories exist
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PROCESSED_FOLDER'], exist_ok=True)
os.makedirs(app.config['TEMP_FOLDER'], exist_ok=True)

# Global storage for processing status
processing_status = {}

# Global storage for downloaded videos (URL -> file path mapping)
downloaded_videos = {}

TEMP_FILE_MAX_AGE = 3 * 3600  # Delete temp files not touched in 3 hours (downloads, clips)
PROCESSED_FILE_MAX_AGE = 24 * 3600  # Delete finished montages after 24 hours


def new_job_id(prefix):
    """Unique ID for a batch, download or process, safe when several users start at once"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def cleanup_old_files():
    """Delete temp and processed files older than their max age. Files that jobs are still
    using are recent, so this is safe to run while other users' jobs are in progress."""
    now = time.time()
    for folder, max_age in [(app.config['TEMP_FOLDER'], TEMP_FILE_MAX_AGE),
                            (app.config['PROCESSED_FOLDER'], PROCESSED_FILE_MAX_AGE)]:
        if not os.path.exists(folder):
            continue
        for file in os.listdir(folder):
            file_path = os.path.join(folder, file)
            try:
                if os.path.isfile(file_path) and now - os.path.getmtime(file_path) > max_age:
                    os.remove(file_path)
                    print(f"Cleaned up old file: {file_path}")
            except Exception as e:
                print(f"Could not remove old file {file_path}: {e}")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per 15 minutes", methods=['POST'], error_message="Too many password attempts.")
def login():
    if not password_required():
        return redirect(url_for('index'))
    
    error = None
    if request.method == 'POST':
        expected_password = os.environ.get('APP_PASSWORD', '')
        entered_password = request.form.get('password', '')
        if not expected_password:
            # Fail closed: the gate is on but no password is set, so nobody gets in
            error = 'The site password has not been set up yet.'
        elif hmac.compare_digest(entered_password.encode(), expected_password.encode()):
            session.permanent = True
            session['auth'] = password_fingerprint()
            return redirect(url_for('index'))
        else:
            error = 'Incorrect password.'
    return render_template('login.html', error=error)

@app.route('/download_multiple', methods=['POST'])
@limiter.limit("5 per hour", error_message="You can make 5 montages per hour.",
               deduct_when=lambda response: response.status_code == 200)  # Only count montages that start
def download_multiple_videos():
    try:
        data = request.get_json()
        urls = data.get('urls', [])
        clip_duration = data.get('clip_duration', 30)  # Default to 30 seconds
        audio_option = data.get('audio_option', 'original')  # Default to original audio
        background_music_url = data.get('background_music_url', '')
        true_achievements_user_id = data.get('true_achievements_user_id', '')
        true_achievements_url = data.get('true_achievements_url', '')
        add_title_overlays = data.get('add_title_overlays', True)  # Default to True
        add_fade_effects = data.get('add_fade_effects', True)  # Default to True
        add_intro_screen = data.get('add_intro_screen', False)  # Default to False
        intro_screen_text = data.get('intro_screen_text', '')  # Default to empty string
        add_clip_transitions = data.get('add_clip_transitions', True)  # Default to True
        selected_games = data.get('selected_games', [])  # Get selected games from TrueAchievements
        
        print(f"DEBUG: Received add_clip_transitions: {add_clip_transitions}")
        print(f"DEBUG: Full request data: {data}")
        
        if not urls:
            return jsonify({'error': 'No URLs provided'}), 400
        
        if not isinstance(urls, list):
            return jsonify({'error': 'URLs must be provided as a list'}), 400
        
        # Validate clip duration
        if not isinstance(clip_duration, int) or clip_duration < 5 or clip_duration > 300:
            return jsonify({'error': 'Clip duration must be between 5 and 300 seconds'}), 400
        
        # Validate audio option
        if audio_option not in ['original', 'background']:
            return jsonify({'error': 'Invalid audio option'}), 400
        
        # Get music source and local music file from request
        music_source = data.get('music_source', 'youtube')
        local_music_file = data.get('local_music_file', '')
        
        # Validate background music if needed
        if audio_option == 'background':
            if music_source == 'youtube' and not background_music_url:
                return jsonify({'error': 'Background music URL required when using YouTube music source'}), 400
            elif music_source == 'local' and not local_music_file:
                return jsonify({'error': 'Local music file required when using local music source'}), 400
        
        # Validate all URLs
        for url in urls:
            if 'youtube.com' not in url and 'youtu.be' not in url:
                return jsonify({'error': f'Invalid YouTube URL: {url}'}), 400
            if 'playlist' in url or 'list=' in url:
                return jsonify({'error': f'Playlist URLs are not supported: {url}'}), 400
        
        print(f"Starting batch download for {len(urls)} videos with {clip_duration}s clips and {audio_option} audio")
        if true_achievements_user_id:
            print(f"TrueAchievements User ID: {true_achievements_user_id}")
            print(f"TrueAchievements URL: {true_achievements_url}")
        
        # Generate unique ID for this batch download
        with _montage_start_lock:
            if running_montage_count() >= MAX_CONCURRENT_MONTAGES:
                return jsonify({'error': 'The server is busy making other montages. Please try again in a minute.'}), 503
            batch_id = new_job_id("batch")
            # Reserve the slot now; the full status is filled in below
            processing_status[batch_id] = {'status': 'downloading', 'started_at': time.time()}
        
        # Initialize batch status
        processing_status[batch_id] = {
            'status': 'downloading',
            'started_at': time.time(),
            'progress': 0,
            'message': f'Starting download of {len(urls)} videos...',
            'total_videos': len(urls),
            'completed_videos': 0,
            'failed_videos': 0,
            'video_paths': [],
            'titles': [],
            'clip_duration': clip_duration,
            'audio_option': audio_option,
            'background_music_url': background_music_url,
            'music_source': music_source,
            'local_music_file': local_music_file,
            'true_achievements_user_id': true_achievements_user_id,
            'true_achievements_url': true_achievements_url,
            'add_title_overlays': add_title_overlays,
            'add_fade_effects': add_fade_effects,
            'add_intro_screen': add_intro_screen,
            'intro_screen_text': intro_screen_text,
            'add_clip_transitions': add_clip_transitions,
            'selected_games': selected_games
        }
        
        # Start batch download in background thread
        thread = threading.Thread(target=download_multiple_videos_thread, args=(urls, batch_id))
        thread.daemon = True
        thread.start()
        
        return jsonify({'batch_id': batch_id, 'message': f'Batch download started for {len(urls)} videos'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download', methods=['POST'])
@limiter.limit("5 per hour", error_message="You can download 5 videos per hour.",
               deduct_when=lambda response: response.status_code == 200)
def download_video():
    try:
        data = request.get_json()
        url = data.get('url')
        
        if not url:
            return jsonify({'error': 'No URL provided'}), 400
        
        # Validate YouTube URL
        if 'youtube.com' not in url and 'youtu.be' not in url:
            return jsonify({'error': 'Please provide a valid YouTube URL'}), 400
        
        # Check if it's a playlist URL and warn user
        if 'playlist' in url or 'list=' in url:
            return jsonify({'error': 'Playlist URLs are not supported. Please use a single video URL.'}), 400
        
        # Generate unique ID for this download
        download_id = new_job_id("download")
        
        # Clean up any existing files with this download ID
        for existing_file in os.listdir(app.config['TEMP_FOLDER']):
            if existing_file.startswith(download_id):
                try:
                    os.remove(os.path.join(app.config['TEMP_FOLDER'], existing_file))
                except:
                    pass
        
        processing_status[download_id] = {
            'status': 'downloading',
            'progress': 0,
            'message': 'Starting download...'
        }
        
        # Start download in background thread
        thread = threading.Thread(target=download_video_thread, args=(url, download_id))
        thread.daemon = True
        thread.start()
        
        # Set a timeout for the download
        def timeout_handler():
            time.sleep(300)  # 5 minutes timeout
            if download_id in processing_status and processing_status[download_id]['status'] == 'downloading':
                processing_status[download_id].update({
                    'status': 'error',
                    'message': 'Download timed out after 5 minutes'
                })
        
        timeout_thread = threading.Thread(target=timeout_handler)
        timeout_thread.daemon = True
        timeout_thread.start()
        
        return jsonify({'download_id': download_id, 'message': 'Download started'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def download_multiple_videos_thread(urls, batch_id):
    """Download multiple videos and automatically extract random 30-second clips (excluding first/last 30s)"""
    try:
        # Check if we have any existing downloaded videos for these URLs
        existing_videos = []
        new_urls = []

        # Downloads only contain the segments for one clip duration, so reuse is keyed on both
        clip_duration = processing_status[batch_id].get('clip_duration', 30)

        for url in urls:
            download_key = f"{url}|{clip_duration}"
            if download_key in downloaded_videos:
                existing_path = downloaded_videos[download_key]
                if os.path.exists(existing_path) and os.path.getsize(existing_path) > 0:
                    existing_videos.append((url, existing_path))
                    os.utime(existing_path)  # Mark as recently used so cleanup keeps it
                    print(f"Found existing download for URL: {url} -> {existing_path}")
                else:
                    # File doesn't exist or is empty, need to download
                    new_urls.append(url)
                    # Remove from tracking
                    del downloaded_videos[download_key]
            else:
                new_urls.append(url)
        
        print(f"Found {len(existing_videos)} existing videos, need to download {len(new_urls)} new videos")
        
        # Remove old files only; other users' jobs may be using the recent ones
        cleanup_old_files()
        
        total_videos = len(urls)
        completed_videos = 0
        failed_videos = 0
        video_paths = []
        titles = []
        existing_paths = dict(existing_videos)
        
        # Title overlays are matched to clips by position, so keep the selected game names
        # aligned with video_paths by leaving out the names of failed downloads
        selected_games = processing_status[batch_id].get('selected_games', [])
        clip_game_titles = []
        
        # Go through the URLs in order so clips stay in the order the games were selected
        for i, url in enumerate(urls):
            processing_status[batch_id]['message'] = f'Downloading video {i + 1}/{total_videos}...'
            video_path = None
            try:
                if url in existing_paths:
                    video_path = existing_paths[url]
                    # Get the title from the file name, minus the "batch_<id>_video_<n>_" prefix
                    filename = os.path.basename(video_path)
                    video_title = re.sub(r'^batch_[0-9a-f]+_video_\d+_', '', filename).rsplit('.', 1)[0]
                    print(f"Reused existing video: {video_title} -> {video_path}")
                else:
                    video_id = f"{batch_id}_video_{i}"
                    video_path, video_title = download_single_video(url, video_id, clip_duration)
                    if video_path:
                        # Track this download for future reuse
                        downloaded_videos[f"{url}|{clip_duration}"] = video_path
                        print(f"Downloaded and tracked video: {url} -> {video_path}")
            except Exception as e:
                print(f"Failed to download video {i + 1}: {e}")
                video_path = None
            
            if video_path:
                video_paths.append(video_path)
                titles.append(video_title)
                if i < len(selected_games):
                    clip_game_titles.append(selected_games[i])
                completed_videos += 1
            else:
                failed_videos += 1
                game_name = selected_games[i] if i < len(selected_games) and selected_games[i] else f"Video {i + 1}"
                processing_status[batch_id].setdefault('skipped_games', []).append(game_name)
            
            processing_status[batch_id].update({
                'completed_videos': completed_videos,
                'failed_videos': failed_videos,
                'progress': int(((i + 1) / total_videos) * 50)  # First 50% for downloads
            })
        
        if selected_games:
            processing_status[batch_id]['selected_games'] = clip_game_titles
        
        # Update batch status with detailed information
        print(f"Batch download completed. Successful: {completed_videos}, Failed: {failed_videos}")
        print(f"Video paths collected: {video_paths}")
        print(f"Titles collected: {titles}")
        
        processing_status[batch_id].update({
            'video_paths': video_paths,
            'titles': titles,
            'completed_videos': completed_videos,
            'failed_videos': failed_videos
        })
        
        if completed_videos > 0 and video_paths:
            # Verify all video paths exist before processing
            valid_paths = []
            valid_titles = []
            valid_game_titles = []
            clip_game_titles = processing_status[batch_id].get('selected_games', [])
            for k, path in enumerate(video_paths):
                if os.path.exists(path) and os.path.getsize(path) > 0:
                    valid_paths.append(path)
                    valid_titles.append(titles[k])
                    if k < len(clip_game_titles):
                        valid_game_titles.append(clip_game_titles[k])
                    print(f"Valid video path: {path} (size: {os.path.getsize(path)} bytes)")
                else:
                    print(f"Invalid video path: {path} (exists: {os.path.exists(path)}, size: {os.path.getsize(path) if os.path.exists(path) else 'N/A'} bytes)")
            # Keep titles aligned with the clips that will actually be processed
            titles = valid_titles
            if clip_game_titles:
                processing_status[batch_id]['selected_games'] = valid_game_titles
            
            if valid_paths:
                # Get clip duration from batch status
                clip_duration = processing_status[batch_id].get('clip_duration', 30)
                
                # Automatically process the videos to extract random clips (excluding first/last 30s)
                processing_status[batch_id]['message'] = f'Starting video processing: extracting {clip_duration}s clips from {len(valid_paths)} videos...'
                processing_status[batch_id]['progress'] = 50
                
                print(f"Starting auto-processing for {len(valid_paths)} valid videos: {valid_paths}")
                
                # Process videos automatically with titles
                process_id = auto_process_videos(valid_paths, batch_id, titles)
                
                if process_id:
                    processing_status[batch_id].update({
                        'status': 'processing',
                        'process_id': process_id,
                        'message': f'Videos downloaded successfully. Starting video processing...'
                    })
                else:
                    processing_status[batch_id].update({
                        'status': 'error',
                        'message': 'Failed to start video processing'
                    })
            else:
                processing_status[batch_id].update({
                    'status': 'error',
                    'message': 'No valid video files found for processing. All downloaded files appear to be invalid.'
                })
        else:
            processing_status[batch_id].update({
                'status': 'error',
                'message': f'No videos were downloaded successfully. Completed: {completed_videos}, Video paths: {len(video_paths)}'
            })
            
    except Exception as e:
        print(f"Batch download error: {str(e)}")
        processing_status[batch_id].update({
            'status': 'error',
            'message': f'Batch download failed: {str(e)}'
        })

# YouTube mostly serves separate video and audio streams now, so download both and let
# yt-dlp merge them with ffmpeg (see merge_output_format in config.py). H.264 is preferred
# because every later ffmpeg step handles it reliably. Combined formats are the fallback.
YT_DOWNLOAD_FORMATS = [
    'bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]',
    'bestvideo[height<=720]+bestaudio',
    'best[height<=720]',
    'best'
]

def join_video_parts(part_paths, output_path):
    """Join video files that share the same codecs into one file without re-encoding"""
    if len(part_paths) == 1:
        os.replace(part_paths[0], output_path)
        return True

    list_path = f"{output_path}.txt"
    with open(list_path, 'w') as f:
        for path in part_paths:
            escaped_path = os.path.abspath(path).replace("'", "'\\''")
            f.write(f"file '{escaped_path}'\n")

    join_cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', '-y', output_path]
    result = subprocess.run(join_cmd, capture_output=True, text=True)
    os.remove(list_path)
    if result.returncode != 0:
        print(f"Joining video parts failed: {result.stderr}")
        return False

    for path in part_paths:
        os.remove(path)
    return True

def download_single_video(url, video_id, clip_duration=None):
    """Download a single video and return the path and title. When clip_duration is given,
    only the segments the montage will use are downloaded (see calculate_clip_segments),
    then joined into one short file, instead of downloading the whole video."""
    try:
        ydl_opts = app.config['YT_DLP_OPTIONS'].copy()
        # Each downloaded segment gets its own file, named by its start time ("NA" for a full download)
        ydl_opts['outtmpl'] = f"{app.config['TEMP_FOLDER']}/{video_id}_part_%(section_start)s.%(ext)s"

        def clip_ranges(info_dict, ydl):
            duration = info_dict.get('duration')
            if not clip_duration or not duration:
                return [{}]  # Unknown length, download the whole video
            segments = calculate_clip_segments(duration, clip_duration)
            print(f"Downloading {len(segments)} segment(s) from {duration}s video: " +
                  ', '.join(f"{start:.1f}s-{start + dur:.1f}s" for start, dur in segments))
            return [{'start_time': start, 'end_time': start + dur} for start, dur in segments]

        ydl_opts['download_ranges'] = clip_ranges
        ydl_opts['force_keyframes_at_cuts'] = True  # Precise cuts so each segment is exactly its length

        # Try different formats if the first attempt fails
        format_options = YT_DOWNLOAD_FORMATS

        download_successful = False
        for i, format_option in enumerate(format_options):
            try:
                ydl_opts['format'] = format_option

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    video_title = info.get('title', 'Unknown')
                    download_successful = True
                    break

            except Exception as format_error:
                print(f"Format {format_option} failed: {format_error}")
                if i == len(format_options) - 1:  # Last attempt
                    raise format_error
                continue

        if not download_successful:
            raise Exception("All download formats failed")

        # Wait a moment for file system to sync
        time.sleep(1)

        # Find the finished segment files (skipping any leftover partial downloads) in time order
        part_pattern = re.compile(rf"^{re.escape(video_id)}_part_([\d.]+|NA)\.(mp4|webm|mkv)$")
        part_files = []
        for filename in os.listdir(app.config['TEMP_FOLDER']):
            match = part_pattern.match(filename)
            if match:
                start = float(match.group(1)) if match.group(1) != 'NA' else 0
                part_files.append((start, os.path.join(app.config['TEMP_FOLDER'], filename)))
        part_paths = [path for _, path in sorted(part_files)]
        print(f"Downloaded parts found for {video_id}: {part_paths}")

        downloaded_files = []
        if part_paths:
            extension = os.path.splitext(part_paths[0])[1]
            joined_path = os.path.join(app.config['TEMP_FOLDER'], f"{video_id}_{yt_dlp.utils.sanitize_filename(video_title)}{extension}")
            if join_video_parts(part_paths, joined_path):
                downloaded_files = [os.path.basename(joined_path)]

        if downloaded_files:
            video_path = os.path.join(app.config['TEMP_FOLDER'], downloaded_files[0])
            
            # Verify the file exists and has content
            if os.path.exists(video_path):
                file_size = os.path.getsize(video_path)
                print(f"Video file verified: {video_path} (size: {file_size} bytes)")
                
                if file_size > 0:
                    return video_path, video_title
                else:
                    print(f"Video file is empty: {video_path}")
                    return None, None
            else:
                print(f"Video file not found at expected path: {video_path}")
                return None, None
        else:
            print(f"No downloaded files found for {video_id}")
            # List all files in temp directory for debugging
            all_files = os.listdir(app.config['TEMP_FOLDER'])
            print(f"All files in temp directory: {all_files}")
            return None, None
            
    except Exception as e:
        print(f"Download error for {video_id}: {str(e)}")
        # Provide more specific error information
        if "Sign in to confirm you're not a bot" in str(e):
            print(f"  Bot detection issue for {video_id} - YouTube is blocking automated access")
        elif "Video unavailable" in str(e):
            print(f"  Video unavailable for {video_id} - may be private or deleted")
        elif "This video is not available" in str(e):
            print(f"  Video not available for {video_id} - may be region-restricted")
        return None, None

def download_video_thread(url, download_id):
    try:
        ydl_opts = app.config['YT_DLP_OPTIONS'].copy()
        ydl_opts['outtmpl'] = f"{app.config['TEMP_FOLDER']}/{download_id}_%(title)s.%(ext)s"
        ydl_opts['progress_hooks'] = [lambda d: progress_hook(d, download_id)]
        ydl_opts['noplaylist'] = True  # Only download single videos, not playlists
        ydl_opts['extract_flat'] = False  # Extract full video info
        
        # Try different formats if the first attempt fails
        format_options = YT_DOWNLOAD_FORMATS
        
        # Try downloading with different formats
        download_successful = False
        for i, format_option in enumerate(format_options):
            try:
                ydl_opts['format'] = format_option
                processing_status[download_id]['message'] = f'Trying format {i+1}/{len(format_options)}: {format_option}'
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    video_title = info.get('title', 'Unknown')
                    
                    processing_status[download_id]['message'] = f'Downloading: {video_title} (format: {format_option})'
                    ydl.download([url])
                    
                    # If we get here, download was successful
                    download_successful = True
                    break
                    
            except Exception as format_error:
                print(f"Format {format_option} failed: {format_error}")
                if i == len(format_options) - 1:  # Last attempt
                    raise format_error
                continue
        
        if not download_successful:
            raise Exception("All download formats failed")
        
        # Wait a moment for file system to sync
        time.sleep(1)
        
        # Find downloaded file
        downloaded_files = [f for f in os.listdir(app.config['TEMP_FOLDER']) if f.startswith(download_id)]
        print(f"Downloaded files found: {downloaded_files}")  # Debug logging
        
        if downloaded_files:
            video_path = os.path.join(app.config['TEMP_FOLDER'], downloaded_files[0])
            processing_status[download_id].update({
                'status': 'completed',
                'progress': 100,
                'message': 'Download completed',
                'video_path': video_path,
                'title': video_title
            })
            
            # Schedule automatic cleanup after 1 minute
            def auto_cleanup_single_download():
                time.sleep(60)  # Wait 1 minute
                try:
                    # Clean up this download's files only
                    if os.path.exists(app.config['TEMP_FOLDER']):
                        for file in os.listdir(app.config['TEMP_FOLDER']):
                            if not file.startswith(download_id):
                                continue
                            file_path = os.path.join(app.config['TEMP_FOLDER'], file)
                            try:
                                if os.path.isfile(file_path):
                                    os.remove(file_path)
                                elif os.path.isdir(file_path):
                                    shutil.rmtree(file_path)
                            except Exception as e:
                                print(f"Could not remove temp file {file_path}: {e}")
                        print(f"Auto-cleanup: Temp files cleared for {download_id}")
                except Exception as e:
                    print(f"Auto-cleanup error: {e}")
            
            cleanup_thread = threading.Thread(target=auto_cleanup_single_download)
            cleanup_thread.daemon = True
            cleanup_thread.start()
        else:
            # List all files in temp directory for debugging
            all_files = os.listdir(app.config['TEMP_FOLDER'])
            print(f"All files in temp directory: {all_files}")  # Debug logging
            
            processing_status[download_id].update({
                'status': 'error',
                'message': f'No video file found after download. Temp directory contents: {all_files}'
            })
                
    except Exception as e:
        print(f"Download error for {download_id}: {str(e)}")  # Debug logging
        
        # Provide helpful error messages for common issues
        error_message = str(e)
        if "Sign in to confirm you're not a bot" in error_message:
            error_message = "YouTube detected automated access. Try using a different video URL or wait a few minutes before trying again."
        elif "Video unavailable" in error_message:
            error_message = "This video is not available for download. It may be private, deleted, or region-restricted."
        elif "This video is not available" in error_message:
            error_message = "This video is not available for download. It may be private, deleted, or region-restricted."
        
        processing_status[download_id].update({
            'status': 'error',
            'message': f'Download failed: {error_message}'
        })

def progress_hook(d, download_id):
    if d['status'] == 'downloading':
        if 'total_bytes' in d and d['total_bytes']:
            progress = (d['downloaded_bytes'] / d['total_bytes']) * 100
            processing_status[download_id]['progress'] = int(progress)
        elif 'total_bytes_estimate' in d and d['total_bytes_estimate']:
            progress = (d['downloaded_bytes'] / d['total_bytes_estimate']) * 100
            processing_status[download_id]['progress'] = int(progress)
    elif d['status'] == 'finished':
        # Download finished, set progress to 100%
        processing_status[download_id]['progress'] = 100
        processing_status[download_id]['message'] = 'Download finished, processing...'
        print(f"Download finished for {download_id}")

@app.route('/status/<download_id>')
def get_status(download_id):
    return jsonify(processing_status.get(download_id, {
        'status': 'not_found',
        'message': 'This job is no longer available, possibly because the server restarted. Please start it again.'
    }))

@app.route('/process', methods=['POST'])
def process_video():
    try:
        data = request.get_json()
        video_path = data.get('video_path')
        cuts = data.get('cuts', [])  # List of [start_time, end_time] in seconds
        background_music = data.get('background_music', '')
        
        if not video_path or not os.path.exists(video_path):
            return jsonify({'error': 'Video file not found'}), 400
        
        # Generate unique ID for processing
        process_id = new_job_id("process")
        processing_status[process_id] = {
            'status': 'processing',
            'progress': 0,
            'message': 'Starting video processing...'
        }
        
        # Start processing in background thread
        thread = threading.Thread(target=process_video_thread, args=(video_path, cuts, background_music, process_id))
        thread.daemon = True
        thread.start()
        
        return jsonify({'process_id': process_id, 'message': 'Processing started'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

def auto_process_videos(video_paths, batch_id, titles=None):
    """Automatically process multiple videos to extract custom duration clips and combine them"""
    try:
        process_id = new_job_id("auto_process")
        
        # Start processing in background thread
        thread = threading.Thread(target=auto_process_videos_thread, args=(video_paths, process_id, batch_id, titles))
        thread.daemon = True
        thread.start()
        
        return process_id
        
    except Exception as e:
        print(f"Failed to start auto processing: {e}")
        return None

# Words that start the non-game part of a YouTube title, like "Gameplay - No Commentary"
VIDEO_TITLE_EXTRAS_PATTERN = re.compile(
    r"\b(gameplay|walkthrough|no commentary|full game|trailer|review|longplay|let'?s play|"
    r"playthrough|first look|part \d+|4k|60 ?fps)\b",
    re.IGNORECASE
)

def game_title_from_video_title(video_title):
    """Best guess at the game name in a YouTube title, for videos with no game name given.
    e.g. "TAMASHIKA - Full Gameplay - No commentary" becomes "TAMASHIKA"
    """
    title = re.sub(r'[(\[{][^)\]}]*[)\]}]', ' ', video_title)  # Bracketed notes like (PS5) or [4K]
    title = re.split(r'\s[-–—]\s|\s*[|｜]\s*', title)[0]  # Text before " - " or "|"
    title = VIDEO_TITLE_EXTRAS_PATTERN.split(title)[0]
    title = ' '.join(title.split()).strip(' -:')
    return title or video_title

def has_audio_stream(video_path):
    """True if the video file has at least one audio track"""
    result = subprocess.run(
        ['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=index', '-of', 'csv=p=0', video_path],
        capture_output=True, text=True
    )
    return bool(result.stdout.strip())

CLIP_SEGMENTS_PER_GAME = 3  # Split each game's clip time into this many segments
MIN_SEGMENT_SECONDS = 3  # Use fewer segments rather than go shorter than this
CLIP_BUFFER_SECONDS = 30  # Skip the first and last 30s of each video (intros/outros)

def calculate_clip_segments(video_duration, clip_duration):
    """Return a list of (start_time, duration) segments that together total clip_duration,
    with start times spread as far apart as possible across the video"""
    if video_duration <= clip_duration:
        # Video is shorter than or equal to requested duration, use the entire video
        return [(0, video_duration)]

    # Exclude the buffers at each end when the video is long enough, otherwise use the whole video
    if video_duration - 2 * CLIP_BUFFER_SECONDS >= clip_duration:
        window_start, window_end = CLIP_BUFFER_SECONDS, video_duration - CLIP_BUFFER_SECONDS
    else:
        window_start, window_end = 0, video_duration

    # Only split when the window after the buffers is at least twice the clip length, so the
    # segments land clearly apart. Shorter videos get one continuous clip from the middle instead.
    if video_duration - 2 * CLIP_BUFFER_SECONDS >= 2 * clip_duration:
        num_segments = max(1, min(CLIP_SEGMENTS_PER_GAME, int(clip_duration // MIN_SEGMENT_SECONDS)))
    else:
        num_segments = 1
    segment_duration = clip_duration / num_segments

    last_start = window_end - segment_duration
    if num_segments == 1:
        # A single segment goes in the middle of the window
        return [((window_start + last_start) / 2, segment_duration)]

    # First segment at the start of the window, last at the end, the rest evenly spaced between.
    # The window is at least clip_duration long, so segments never overlap.
    spacing = (last_start - window_start) / (num_segments - 1)
    return [(window_start + n * spacing, segment_duration) for n in range(num_segments)]

def auto_process_videos_thread(video_paths, process_id, batch_id, titles=None):
    """Process videos to extract random 30-second clips (excluding first/last 30s) and combine them"""
    try:
        print(f"Starting auto-processing for {len(video_paths)} videos: {video_paths}")
        
        # Clean up any existing clip files to prevent reuse
        print(f"Cleaning up any existing clip files for process {process_id}...")
        try:
            if os.path.exists(app.config['TEMP_FOLDER']):
                for file in os.listdir(app.config['TEMP_FOLDER']):
                    if file.startswith(f'temp_clip_{process_id}_') or file.startswith(f'concat_list_{process_id}'):
                        file_path = os.path.join(app.config['TEMP_FOLDER'], file)
                        try:
                            if os.path.isfile(file_path):
                                os.remove(file_path)
                                print(f"Cleaned up existing clip file: {file}")
                        except Exception as e:
                            print(f"Could not remove clip file {file_path}: {e}")
                print("Clip files cleaned for new process")
        except Exception as e:
            print(f"Error during clip cleanup: {e}")
        
        processing_status[process_id] = {
            'status': 'processing',
            'progress': 0,
            'message': 'Loading videos...',
            'batch_id': batch_id
        }
        
        # Process each video to extract random 30-second clips (excluding first/last 30s) using FFmpeg directly
        temp_clip_files = []
        total_videos = len(video_paths)
        
        for i, video_path in enumerate(video_paths):
            clips_before = len(temp_clip_files)
            game_title = f"Game {i+1}"
            try:
                # Get game title for overlay - use the game name sent with this video when there is one,
                # otherwise work the game name out of the YouTube video title
                batch_status = processing_status.get(batch_id, {})
                selected_games = batch_status.get('selected_games', [])
                
                if i < len(selected_games) and selected_games[i]:
                    game_title = selected_games[i]
                    print(f"✅ Using selected game title for clip {i+1}: {game_title}")
                elif titles and i < len(titles):
                    game_title = game_title_from_video_title(titles[i])
                    print(f"📺 Using game title from YouTube title for clip {i+1}: {game_title} (from: {titles[i]})")
                else:
                    game_title = f"Game {i+1}"
                    print(f"⚠️ Using fallback title for clip {i+1}: {game_title}")
                
                logging.info(f"=== Processing video {i+1}/{total_videos} ===")
                logging.info(f"Video path: {video_path}")
                logging.info(f"Video title: {titles[i] if titles and i < len(titles) else 'Unknown'}")
                print(f"=== Processing video {i+1}/{total_videos} ===")
                print(f"Video path: {video_path}")
                print(f"Video title: {titles[i] if titles and i < len(titles) else 'Unknown'}")
                
                # Check if file exists and is readable
                if not os.path.exists(video_path):
                    print(f"Video file not found: {video_path}")
                    continue
                
                file_size = os.path.getsize(video_path)
                print(f"Video file size: {file_size} bytes")
                
                if file_size == 0:
                    print(f"Video file is empty: {video_path}")
                    continue
                
                # Get customization options from batch status
                batch_status = processing_status.get(batch_id, {})
                clip_duration = batch_status.get('clip_duration', 30)
                
                processing_status[process_id]['message'] = f'Extracting {clip_duration}s clip from video {i+1}/{total_videos}...'
                processing_status[process_id]['progress'] = 50 + int((i / total_videos) * 30)  # 50-80% for clip extraction
                
                # Also update the batch status to show progress
                if batch_id in processing_status:
                    processing_status[batch_id]['progress'] = 50 + int((i / total_videos) * 30)
                    processing_status[batch_id]['message'] = f'Extracting {clip_duration}s clips from {total_videos} videos... ({i+1}/{total_videos} complete)'
                
                # Get video duration using ffprobe
                duration_cmd = [
                    'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1', video_path
                ]
                
                try:
                    result = subprocess.run(duration_cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        video_duration = float(result.stdout.strip())
                        print(f"Video duration: {video_duration} seconds")
                    else:
                        print(f"Could not get video duration, assuming 60 seconds")
                        video_duration = 60.0
                except Exception as e:
                    print(f"Error getting duration: {e}, assuming 60 seconds")
                    video_duration = 60.0
                
                # Split the clip into segments spread across the video (excluding first and last 30s)
                segments = calculate_clip_segments(video_duration, clip_duration)
                segment_summary = ', '.join(f"{start:.2f}s-{start + dur:.2f}s" for start, dur in segments)
                print(f"Extracting {len(segments)} segment(s) totaling {sum(dur for _, dur in segments):.2f}s: {segment_summary}")

                # Extract the segments and join them into one clip for this game
                temp_clip_path = os.path.join(app.config['TEMP_FOLDER'], f'temp_clip_{process_id}_{i}.mp4')
                
                # Write the title to a file for drawtext to read. Putting it inline would need
                # escaping, and titles with apostrophes (e.g. "Assassin's Creed") broke the command.
                title_file_path = os.path.join(app.config['TEMP_FOLDER'], f'title_{process_id}_{i}.txt')
                with open(title_file_path, 'w', encoding='utf-8') as f:
                    f.write(game_title[:50])  # Limit length
                
                # Check if title overlays are enabled
                batch_status = processing_status.get(batch_id, {})
                add_title_overlays = batch_status.get('add_title_overlays', True)
                
                # Open the video once per segment with input seeking (fast and frame-accurate when
                # re-encoding), then concat the segments so audio and video stay in sync
                extract_cmd = ['ffmpeg']
                for start, dur in segments:
                    extract_cmd += ['-ss', f'{start:.3f}', '-t', f'{dur:.3f}', '-i', video_path]
                
                # Every clip needs an audio track to be joined with the others, so give videos
                # with no sound a silent track of the same length for each segment
                if has_audio_stream(video_path):
                    audio_labels = [f'[{n}:a:0]' for n in range(len(segments))]
                else:
                    print(f"Video has no audio track, adding silence: {video_path}")
                    audio_labels = []
                    for n, (start, dur) in enumerate(segments):
                        extract_cmd += ['-f', 'lavfi', '-t', f'{dur:.3f}', '-i', 'anullsrc=channel_layout=stereo:sample_rate=48000']
                        audio_labels.append(f'[{len(segments) + n}:a:0]')

                concat_inputs = ''.join(f'[{n}:v:0]{audio_labels[n]}' for n in range(len(segments)))
                filter_complex = f'{concat_inputs}concat=n={len(segments)}:v=1:a=1[joinedv][outa]'
                if add_title_overlays:
                    # Add text overlay in center bottom with black background
                    # expansion=none shows the text as-is, so "%" in a title isn't treated as a code
                    filter_complex += f';[joinedv]drawtext=textfile=\'{title_file_path}\':expansion=none:fontcolor=white:fontsize=12:box=1:boxcolor=black@0.5:boxborderw=3:x=(w-text_w)/2:y=h-text_h-10[outv]'
                else:
                    filter_complex += ';[joinedv]null[outv]'

                extract_cmd += [
                    '-filter_complex', filter_complex,
                    '-map', '[outv]', '-map', '[outa]',
                    '-c:v', 'libx264', '-c:a', 'aac', '-y', temp_clip_path
                ]
                
                print(f"Running FFmpeg extract command: {' '.join(extract_cmd)}")
                extract_result = subprocess.run(extract_cmd, capture_output=True, text=True)
                os.remove(title_file_path)
                
                if extract_result.returncode == 0:
                    if os.path.exists(temp_clip_path) and os.path.getsize(temp_clip_path) > 0:
                        # Verify the actual duration of the extracted clip
                        duration_cmd = [
                            'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1', temp_clip_path
                        ]
                        try:
                            duration_result = subprocess.run(duration_cmd, capture_output=True, text=True)
                            if duration_result.returncode == 0:
                                actual_duration = float(duration_result.stdout.strip())
                                print(f"✅ Successfully extracted clip {i+1} to {temp_clip_path} (actual duration: {actual_duration:.2f}s)")
                            else:
                                print(f"✅ Successfully extracted clip {i+1} to {temp_clip_path} (could not verify duration)")
                        except Exception as e:
                            print(f"✅ Successfully extracted clip {i+1} to {temp_clip_path} (duration check failed: {e})")
                        
                        temp_clip_files.append(temp_clip_path)
                        logging.info(f"✅ Successfully added clip {i+1} to processing list")
                    else:
                        logging.error(f"❌ FFmpeg extract succeeded but output file is invalid for video {i+1}: {video_path}")
                        logging.error(f"   File exists: {os.path.exists(temp_clip_path)}, Size: {os.path.getsize(temp_clip_path) if os.path.exists(temp_clip_path) else 'N/A'}")
                        print(f"❌ FFmpeg extract succeeded but output file is invalid for video {i+1}: {video_path}")
                        print(f"   File exists: {os.path.exists(temp_clip_path)}, Size: {os.path.getsize(temp_clip_path) if os.path.exists(temp_clip_path) else 'N/A'}")
                        continue
                else:
                    logging.error(f"❌ FFmpeg extract failed for video {i+1}: {video_path}")
                    logging.error(f"   Error: {extract_result.stderr}")
                    print(f"❌ FFmpeg extract failed for video {i+1}: {video_path}")
                    print(f"   Error: {extract_result.stderr}")
                    continue
                
            except Exception as e:
                print(f"Error processing video {i+1} ({video_path}): {e}")
                traceback.print_exc()
                continue
            finally:
                # No clip was added for this video, so tell the user which game is missing
                if len(temp_clip_files) == clips_before and batch_id in processing_status:
                    processing_status[batch_id].setdefault('skipped_games', []).append(game_title)
        
        if not temp_clip_files:
            print(f"No clips were successfully processed. Total videos attempted: {total_videos}")
            processing_status[process_id].update({
                'status': 'error',
                'message': f'No videos could be processed. Attempted {total_videos} videos but all failed.'
            })
            return
        
        print(f"Successfully extracted {len(temp_clip_files)} clips from {total_videos} videos")
        print(f"Clip files: {temp_clip_files}")
        
        processing_status[process_id]['message'] = 'Preparing to combine video clips...'
        processing_status[process_id]['progress'] = 80
        
        # Also update the batch status
        if batch_id in processing_status:
            processing_status[batch_id]['progress'] = 80
            processing_status[batch_id]['message'] = f'All {len(temp_clip_files)} clips extracted successfully. Preparing to combine...'
        
        # Validate all temp clip files
        valid_clip_files = [f for f in temp_clip_files if os.path.exists(f) and os.path.getsize(f) > 0]
        print(f"Valid clip files for concatenation: {len(valid_clip_files)} out of {len(temp_clip_files)}")
        
        # Debug: Check each clip file
        for i, clip_file in enumerate(temp_clip_files):
            if os.path.exists(clip_file):
                size = os.path.getsize(clip_file)
                print(f"Clip {i+1}: {clip_file} (size: {size} bytes) - {'VALID' if size > 0 else 'INVALID'}")
            else:
                print(f"Clip {i+1}: {clip_file} - MISSING")
        
        # Debug: Check which clips made it to valid list
        print(f"Valid clips that will be concatenated:")
        for i, clip_file in enumerate(valid_clip_files):
            print(f"  {i+1}: {clip_file}")
        
        # Debug each clip file
        for i, clip_file in enumerate(valid_clip_files):
            file_size = os.path.getsize(clip_file)
            print(f"Clip {i+1}: {clip_file} (size: {file_size} bytes)")
        
        if not valid_clip_files:
            print("No valid clip files to concatenate")
            processing_status[process_id].update({
                'status': 'error',
                'message': 'No valid video clip files could be extracted for concatenation'
            })
            return
        
        # Get audio options from batch status
        batch_status = processing_status.get(batch_id, {})
        audio_option = batch_status.get('audio_option', 'original')
        background_music_url = batch_status.get('background_music_url', '')
        
        # Debug logging for audio options
        print(f"DEBUG: Audio option from batch status: '{audio_option}'")
        print(f"DEBUG: Background music URL from batch status: '{background_music_url}'")
        print(f"DEBUG: Full batch status: {batch_status}")
        
                # Concatenate all clips using FFmpeg
        if len(valid_clip_files) > 1:
            print(f"Concatenating {len(valid_clip_files)} clips using FFmpeg...")
            
            # Create a text file listing all clips for ffmpeg
            concat_list_path = os.path.join(app.config['TEMP_FOLDER'], f'concat_list_{process_id}.txt')
            print(f"Creating concat list at: {os.path.abspath(concat_list_path)}")
            
            with open(concat_list_path, 'w') as f:
                for temp_file in valid_clip_files:
                    # Use absolute paths to avoid path resolution issues
                    abs_path = os.path.abspath(temp_file)
                    f.write(f"file '{abs_path}'\n")
                    print(f"Added to concat list: {abs_path}")
            
            # Debug: show the contents of the concat list file
            print("Concat list file contents:")
            with open(concat_list_path, 'r') as f:
                print(f.read())
            
            # Get clip transition option from batch status
            add_clip_transitions = batch_status.get('add_clip_transitions', True)
            print(f"DEBUG: Clip transitions enabled: {add_clip_transitions}")
            print(f"DEBUG: Batch status keys: {list(batch_status.keys())}")
            print(f"DEBUG: Full batch status: {batch_status}")
            
            # Use a different approach: concatenate videos sequentially with optional black fade transitions
            temp_output_path = os.path.join(app.config['TEMP_FOLDER'], f'temp_combined_{process_id}.mp4')
            
            # Create clips with fade transitions
            if add_clip_transitions:
                print("Creating clips with fade transitions...")
                processing_status[process_id]['message'] = 'Adding fade effects to individual clips...'
                processing_status[process_id]['progress'] = 82
                if batch_id in processing_status:
                    processing_status[batch_id]['progress'] = 82
                    processing_status[batch_id]['message'] = 'Adding fade effects to individual clips...'
                
                # Create a list of all clips with fade effects
                fade_clips = []
                for i, clip_path in enumerate(valid_clip_files):
                    fade_clip_path = os.path.join(app.config['TEMP_FOLDER'], f'fade_clip_{process_id}_{i}.mp4')
                    
                    # Get clip duration
                    duration_cmd = [
                        'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1', clip_path
                    ]
                    try:
                        duration_result = subprocess.run(duration_cmd, capture_output=True, text=True)
                        if duration_result.returncode == 0:
                            clip_duration = float(duration_result.stdout.strip())
                        else:
                            clip_duration = 30.0  # Default duration
                    except:
                        clip_duration = 30.0
                    
                    # For fade transitions, we need to handle each clip differently:
                    # - First clip: fade in at start, fade out at end
                    # - Middle clips: fade in at start, fade out at end  
                    # - Last clip: fade in at start, fade out at end
                    fade_duration = 1.0
                    
                    # Add fade in/out to each clip (1 second each)
                    fade_cmd = [
                        'ffmpeg', '-i', clip_path,
                        '-vf', f'fade=t=in:st=0:d={fade_duration},fade=t=out:st={clip_duration-fade_duration}:d={fade_duration}',
                        '-c:v', 'libx264', '-c:a', 'aac', '-y', fade_clip_path
                    ]
                    
                    print(f"Adding fade effects to clip {i+1}: {' '.join(fade_cmd)}")
                    fade_result = subprocess.run(fade_cmd, capture_output=True, text=True)
                    
                    if fade_result.returncode == 0:
                        fade_clips.append(fade_clip_path)
                        print(f"Fade clip {i+1} created successfully")
                    else:
                        print(f"Failed to create fade clip {i+1}, using original: {fade_result.stderr}")
                        fade_clips.append(clip_path)  # Fallback to original clip
                
                # Now concatenate all fade clips with crossfade transitions
                if len(fade_clips) > 1:
                    print("Creating crossfade transitions between clips...")
                    processing_status[process_id]['message'] = 'Creating crossfade transitions between clips...'
                    processing_status[process_id]['progress'] = 85
                    if batch_id in processing_status:
                        processing_status[batch_id]['progress'] = 85
                        processing_status[batch_id]['message'] = 'Creating crossfade transitions between clips...'
                    
                    # Start with the first clip
                    current_output = fade_clips[0]
                    
                    # Process each subsequent clip with crossfade
                    for i in range(1, len(fade_clips)):
                        next_clip = fade_clips[i]
                        temp_combined = os.path.join(app.config['TEMP_FOLDER'], f'crossfade_{process_id}_step_{i}.mp4')
                        
                        # Update progress for crossfade step - ensure it only goes forward
                        crossfade_progress = 85 + int((i / len(fade_clips)) * 10)  # 85-95% for crossfade
                        current_progress = processing_status[process_id].get('progress', 0)
                        if crossfade_progress > current_progress:
                            processing_status[process_id]['message'] = f'Creating crossfade transition {i}/{len(fade_clips)-1}...'
                            processing_status[process_id]['progress'] = crossfade_progress
                            
                            # Also update the batch status
                            if batch_id in processing_status:
                                processing_status[batch_id]['progress'] = crossfade_progress
                                processing_status[batch_id]['message'] = f'Creating crossfade transition {i}/{len(fade_clips)-1}...'
                        
                        # Create a simple fade transition by concatenating with overlap
                        # First, trim the current output to remove the last 1 second
                        trimmed_current = os.path.join(app.config['TEMP_FOLDER'], f'trimmed_current_{process_id}_{i}.mp4')
                        
                        # Get duration of current output
                        duration_cmd = [
                            'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1', current_output
                        ]
                        try:
                            duration_result = subprocess.run(duration_cmd, capture_output=True, text=True)
                            if duration_result.returncode == 0:
                                current_duration = float(duration_result.stdout.strip())
                                trim_duration = max(0, current_duration - 1.0)  # Remove last 1 second
                            else:
                                trim_duration = 30.0  # Default duration
                        except:
                            trim_duration = 30.0
                        
                        # Trim current output
                        trim_cmd = [
                            'ffmpeg', '-i', current_output, '-t', str(trim_duration),
                            '-c:v', 'copy', '-c:a', 'copy', '-y', trimmed_current
                        ]
                        
                        print(f"Trimming current output: {' '.join(trim_cmd)}")
                        trim_result = subprocess.run(trim_cmd, capture_output=True, text=True)
                        
                        if trim_result.returncode == 0:
                            # Now concatenate the trimmed current with the next clip
                            concat_cmd = [
                                'ffmpeg', '-i', trimmed_current, '-i', next_clip,
                                '-filter_complex', '[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]',
                                '-map', '[outv]', '-map', '[outa]',
                                '-c:v', 'libx264', '-c:a', 'aac', '-y', temp_combined
                            ]
                            
                            print(f"Concatenating with fade transition: {' '.join(concat_cmd)}")
                            concat_result = subprocess.run(concat_cmd, capture_output=True, text=True)
                            
                            if concat_result.returncode == 0:
                                # Clean up trimmed file
                                if os.path.exists(trimmed_current):
                                    os.remove(trimmed_current)
                            else:
                                print(f"Concatenation failed: {concat_result.stderr}")
                                # Fallback: just concatenate without trimming
                                fallback_cmd = [
                                    'ffmpeg', '-i', current_output, '-i', next_clip,
                                    '-filter_complex', '[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]',
                                    '-map', '[outv]', '-map', '[outa]',
                                    '-c:v', 'libx264', '-c:a', 'aac', '-y', temp_combined
                                ]
                                print(f"Fallback concatenation: {' '.join(fallback_cmd)}")
                                fallback_result = subprocess.run(fallback_cmd, capture_output=True, text=True)
                                if fallback_result.returncode != 0:
                                    print(f"Fallback also failed: {fallback_result.stderr}")
                                    break
                        else:
                            print(f"Trimming failed: {trim_result.stderr}")
                            # Fallback: just concatenate without trimming
                            fallback_cmd = [
                                'ffmpeg', '-i', current_output, '-i', next_clip,
                                '-filter_complex', '[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]',
                                '-map', '[outv]', '-map', '[outa]',
                                '-c:v', 'libx264', '-c:a', 'aac', '-y', temp_combined
                            ]
                            print(f"Fallback concatenation: {' '.join(fallback_cmd)}")
                            fallback_result = subprocess.run(fallback_cmd, capture_output=True, text=True)
                            if fallback_result.returncode != 0:
                                print(f"Fallback also failed: {fallback_result.stderr}")
                                break
                        
                        # The concatenation is already done above, just check the result
                        if concat_result.returncode == 0 or fallback_result.returncode == 0:
                            # Clean up previous temp file
                            if i > 1:  # Don't delete the first clip file yet
                                os.remove(current_output)
                            current_output = temp_combined
                            print(f"Fade transition {i} successful")
                            
                            # Update progress after successful fade step - ensure it only goes forward
                            crossfade_progress = 85 + int(((i + 1) / len(fade_clips)) * 10)  # 85-95% for fade
                            current_progress = processing_status[process_id].get('progress', 0)
                            if crossfade_progress > current_progress:
                                processing_status[process_id]['message'] = f'Fade transition {i}/{len(fade_clips)-1} completed'
                                processing_status[process_id]['progress'] = crossfade_progress
                                
                                # Also update the batch status
                                if batch_id in processing_status:
                                    processing_status[batch_id]['progress'] = crossfade_progress
                                    processing_status[batch_id]['message'] = f'Fade transition {i}/{len(fade_clips)-1} completed'
                        else:
                            print(f"Fade transition {i} failed")
                            print("Falling back to normal concatenation...")
                            # Fallback to normal concatenation
                            break
                    
                    # Move final result to temp_output_path
                    shutil.move(current_output, temp_output_path)
                    print("Crossfade transitions completed successfully!")
                    
                    # Clean up fade clips
                    for fade_clip in fade_clips:
                        if os.path.exists(fade_clip):
                            os.remove(fade_clip)
                else:
                    # Single clip, just copy it
                    shutil.copy2(fade_clips[0], temp_output_path)
                    print("Single fade clip copied to output")
                
            else:
                # Normal concatenation without fade transitions
                print("Using normal concatenation without fade transitions...")
                processing_status[process_id]['message'] = 'Concatenating video clips...'
                processing_status[process_id]['progress'] = 85
                if batch_id in processing_status:
                    processing_status[batch_id]['progress'] = 85
                    processing_status[batch_id]['message'] = 'Concatenating video clips...'
                
                # Start with the first clip
                current_output = valid_clip_files[0]
                
                # Concatenate all clips normally
                for i in range(1, len(valid_clip_files)):
                    next_clip = valid_clip_files[i]
                    temp_combined = os.path.join(app.config['TEMP_FOLDER'], f'temp_combined_{process_id}_step_{i}.mp4')
                    
                    # Update progress for concatenation step - ensure it only goes forward
                    concat_progress = 85 + int((i / len(valid_clip_files)) * 10)  # 85-95% for concatenation
                    current_progress = processing_status[process_id].get('progress', 0)
                    if concat_progress > current_progress:
                        processing_status[process_id]['message'] = f'Concatenating clips {i}/{len(valid_clip_files)-1}...'
                        processing_status[process_id]['progress'] = concat_progress
                        
                        # Also update the batch status
                        if batch_id in processing_status:
                            processing_status[batch_id]['progress'] = concat_progress
                            processing_status[batch_id]['message'] = f'Concatenating clips {i}/{len(valid_clip_files)-1}...'
                    
                    # Concatenate clips normally
                    concat_cmd = [
                        'ffmpeg', '-i', current_output, '-i', next_clip,
                        '-filter_complex', '[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]',
                        '-map', '[outv]', '-map', '[outa]',
                        '-c:v', 'libx264', '-c:a', 'aac', '-y', temp_combined
                    ]
                    
                    print(f"Running FFmpeg concatenation step {i}: {' '.join(concat_cmd)}")
                    result = subprocess.run(concat_cmd, capture_output=True, text=True)
                    
                    if result.returncode == 0:
                        # Clean up previous temp file
                        if i > 1:  # Don't delete the first clip file yet
                            os.remove(current_output)
                        current_output = temp_combined
                        print(f"Concatenation step {i} successful")
                        
                        # Update progress after successful concatenation step - ensure it only goes forward
                        concat_progress = 85 + int(((i + 1) / len(valid_clip_files)) * 10)  # 85-95% for concatenation
                        current_progress = processing_status[process_id].get('progress', 0)
                        if concat_progress > current_progress:
                            processing_status[process_id]['message'] = f'Concatenated clips {i}/{len(valid_clip_files)-1} successfully'
                            processing_status[process_id]['progress'] = concat_progress
                            
                            # Also update the batch status
                            if batch_id in processing_status:
                                processing_status[batch_id]['progress'] = concat_progress
                                processing_status[batch_id]['message'] = f'Concatenated clips {i}/{len(valid_clip_files)-1} successfully'
                    else:
                        print(f"Concatenation step {i} failed: {result.stderr}")
                        raise Exception(f"Concatenation step {i} failed: {result.stderr}")
                
                # Move final result to temp_output_path
                shutil.move(current_output, temp_output_path)
                print("Normal concatenation completed successfully!")
            
            # Verify the concatenated video duration
            duration_cmd = [
                'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1', temp_output_path
            ]
            try:
                duration_result = subprocess.run(duration_cmd, capture_output=True, text=True)
                if duration_result.returncode == 0:
                    final_duration = float(duration_result.stdout.strip())
                    print(f"Concatenated video duration: {final_duration:.2f}s (expected: {len(valid_clip_files) * clip_duration:.2f}s)")
                else:
                    print("Could not verify concatenated video duration")
            except Exception as e:
                print(f"Duration verification failed: {e}")
            
            # Create intro screen if enabled
            batch_status = processing_status.get(batch_id, {})
            add_intro_screen = batch_status.get('add_intro_screen', False)
            intro_screen_text = batch_status.get('intro_screen_text', '')
            
            print(f"DEBUG: Intro screen check - add_intro_screen: {add_intro_screen}, intro_screen_text: '{intro_screen_text}'")
            print(f"DEBUG: Full batch status keys: {list(batch_status.keys())}")
            
            if add_intro_screen and intro_screen_text:
                print(f"Creating intro screen with text: '{intro_screen_text}'")
                intro_screen_path = os.path.join(app.config['TEMP_FOLDER'], f'intro_screen_{process_id}.mp4')
                
                # Create a 3-second black video with white text using a more reliable approach
                # First create a simple black video
                black_video_cmd = [
                    'ffmpeg', '-f', 'lavfi', '-i', 'color=black:size=1920x1080:duration=3',
                    '-c:v', 'libx264', '-y', intro_screen_path
                ]
                
                print(f"Running FFmpeg black video command: {' '.join(black_video_cmd)}")
                black_result = subprocess.run(black_video_cmd, capture_output=True, text=True)
                
                if black_result.returncode == 0:
                    print("Black video created successfully!")
                    
                    # Now add text overlay to the black video
                    text_overlay_cmd = [
                        'ffmpeg', '-i', intro_screen_path,
                        '-vf', f'drawtext=text=\'{intro_screen_text}\':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2',
                        '-c:v', 'libx264', '-c:a', 'aac', '-y', os.path.join(app.config['TEMP_FOLDER'], f'intro_with_text_{process_id}.mp4')
                    ]
                    
                    print(f"Running FFmpeg text overlay command: {' '.join(text_overlay_cmd)}")
                    text_result = subprocess.run(text_overlay_cmd, capture_output=True, text=True)
                    
                    if text_result.returncode == 0:
                        print("Text overlay added successfully!")
                        # Update intro screen path to the one with text
                        intro_screen_path = os.path.join(app.config['TEMP_FOLDER'], f'intro_with_text_{process_id}.mp4')
                        
                        # Concatenate intro screen with the main video
                        temp_with_intro = os.path.join(app.config['TEMP_FOLDER'], f'temp_with_intro_{process_id}.mp4')
                        intro_concat_cmd = [
                            'ffmpeg', '-i', intro_screen_path, '-i', temp_output_path,
                            '-filter_complex', '[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]',
                            '-map', '[outv]', '-map', '[outa]',
                            '-c:v', 'libx264', '-c:a', 'aac', '-y', temp_with_intro
                        ]
                        
                        print(f"Running FFmpeg intro concatenation: {' '.join(intro_concat_cmd)}")
                        intro_concat_result = subprocess.run(intro_concat_cmd, capture_output=True, text=True)
                        
                        if intro_concat_result.returncode == 0:
                            print("Intro screen concatenated successfully!")
                            # Use the video with intro for further processing
                            temp_output_path = temp_with_intro
                            # Clean up the separate intro file
                            if os.path.exists(intro_screen_path):
                                os.remove(intro_screen_path)
                        else:
                            print(f"Failed to concatenate intro screen: {intro_concat_result.stderr}")
                            print("Continuing without intro screen...")
                    else:
                        print(f"Failed to add text overlay: {text_result.stderr}")
                        print("Continuing without intro screen...")
                else:
                    print(f"Failed to create black video: {black_result.stderr}")
                    print("Continuing without intro screen...")
            else:
                print("Intro screen disabled or no text provided, skipping...")
            
            # Add fade in/out effects to the concatenated video (if enabled)
            add_fade_effects = batch_status.get('add_fade_effects', True)
            
            if add_fade_effects:
                print("Adding fade in/out effects...")
                current_progress = processing_status[process_id].get('progress', 0)
                if 95 > current_progress:
                    processing_status[process_id]['message'] = 'Adding fade in/out effects to final video...'
                    processing_status[process_id]['progress'] = 95
                    if batch_id in processing_status:
                        processing_status[batch_id]['progress'] = 95
                        processing_status[batch_id]['message'] = 'Adding fade in/out effects to final video...'
                temp_with_fades = os.path.join(app.config['TEMP_FOLDER'], f'temp_with_fades_{process_id}.mp4')
                
                # Get video duration for fade calculations
                duration_cmd = [
                    'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1', temp_output_path
                ]
                try:
                    duration_result = subprocess.run(duration_cmd, capture_output=True, text=True)
                    if duration_result.returncode == 0:
                        video_duration = float(duration_result.stdout.strip())
                        print(f"Video duration for fade effects: {video_duration:.2f}s")
                        
                        # Add 1 second fade in at start and 1 second fade out at end
                        fade_cmd = [
                            'ffmpeg', '-i', temp_output_path,
                            '-vf', f'fade=t=in:st=0:d=1,fade=t=out:st={video_duration-1}:d=1',
                            '-c:v', 'libx264', '-c:a', 'aac', '-y', temp_with_fades
                        ]
                        
                        print(f"Running FFmpeg fade command: {' '.join(fade_cmd)}")
                        fade_result = subprocess.run(fade_cmd, capture_output=True, text=True)
                        
                        if fade_result.returncode == 0:
                            print("Fade effects added successfully!")
                            # Use the video with fades for further processing
                            temp_output_path = temp_with_fades
                        else:
                            print(f"Failed to add fade effects: {fade_result.stderr}")
                            print("Continuing without fade effects...")
                    else:
                        print("Could not get video duration for fade effects, continuing without fades...")
                except Exception as e:
                    print(f"Error adding fade effects: {e}, continuing without fades...")
            else:
                print("Fade effects disabled, skipping...")
            
            # Handle audio options
            final_output_path = os.path.join(app.config['PROCESSED_FOLDER'], f'auto_combined_{process_id}.mp4')
            
            # Get music source and file from batch status
            batch_status = processing_status.get(batch_id, {})
            music_source = batch_status.get('music_source', 'youtube')
            local_music_file = batch_status.get('local_music_file', '')
            
            print(f"DEBUG: Processing audio option: '{audio_option}' with music source: '{music_source}'")
            print(f"DEBUG: Background music URL: '{background_music_url}', Local music file: '{local_music_file}'")
            
            if audio_option == 'background' and (background_music_url or local_music_file):
                print("Replacing original audio with background music...")
                current_progress = processing_status[process_id].get('progress', 0)
                if 98 > current_progress:
                    processing_status[process_id]['message'] = 'Replacing audio with background music...'
                    processing_status[process_id]['progress'] = 98
                    if batch_id in processing_status:
                        processing_status[batch_id]['progress'] = 98
                        processing_status[batch_id]['message'] = 'Replacing audio with background music...'
                
                # Get background music path based on source
                background_music_path = None
                random_start_time = 0  # Default for YouTube music
                
                if music_source == 'youtube' and background_music_url:
                    # Download background music from YouTube
                    background_music_path = download_background_music(background_music_url, process_id)
                    if not background_music_path:
                        print("Failed to download background music from YouTube, using video without music")
                        shutil.copy2(temp_output_path, final_output_path)
                        return
                elif music_source == 'local' and local_music_file:
                    # Use local music file
                    music_folder = app.config['MUSIC_FOLDER']
                    background_music_path = os.path.join(music_folder, local_music_file)
                    if not os.path.exists(background_music_path):
                        print(f"Local music file not found: {background_music_path}, using video without music")
                        shutil.copy2(temp_output_path, final_output_path)
                        return
                    
                    # Get music file duration and calculate random start time
                    music_duration_cmd = [
                        'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1', background_music_path
                    ]
                    try:
                        duration_result = subprocess.run(music_duration_cmd, capture_output=True, text=True)
                        if duration_result.returncode == 0:
                            music_duration = float(duration_result.stdout.strip())
                            # Get video duration to determine how much music we need
                            video_duration_cmd = [
                                'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                                '-of', 'default=noprint_wrappers=1:nokey=1', temp_output_path
                            ]
                            video_duration_result = subprocess.run(video_duration_cmd, capture_output=True, text=True)
                            if video_duration_result.returncode == 0:
                                video_duration = float(video_duration_result.stdout.strip())
                                
                                # Calculate random start time (leave some buffer at the end)
                                max_start_time = max(0, music_duration - video_duration - 10)  # 10 second buffer
                                if max_start_time > 0:
                                    import random
                                    random_start_time = random.uniform(0, max_start_time)
                                    print(f"Using local music file: {background_music_path}")
                                    print(f"Music duration: {music_duration:.2f}s, Video duration: {video_duration:.2f}s")
                                    print(f"Starting music at random time: {random_start_time:.2f}s")
                                else:
                                    random_start_time = 0
                                    print(f"Using local music file: {background_music_path} (from beginning)")
                            else:
                                random_start_time = 0
                                print(f"Using local music file: {background_music_path} (could not get video duration)")
                        else:
                            random_start_time = 0
                            print(f"Using local music file: {background_music_path} (could not get music duration)")
                    except Exception as e:
                        random_start_time = 0
                        print(f"Using local music file: {background_music_path} (error getting duration: {e})")
                else:
                    print("No valid music source found, using video without music")
                    shutil.copy2(temp_output_path, final_output_path)
                    return
                
                # Replace original audio with background music
                if random_start_time > 0:
                    # Use random start time for local music
                    music_cmd = [
                        'ffmpeg', '-i', temp_output_path, '-ss', str(random_start_time), '-i', background_music_path,
                        '-c:v', 'copy', '-c:a', 'aac', '-map', '0:v', '-map', '1:a',
                        '-shortest', '-y', final_output_path
                    ]
                else:
                    # Use from beginning (YouTube music or local music fallback)
                    music_cmd = [
                        'ffmpeg', '-i', temp_output_path, '-i', background_music_path,
                        '-c:v', 'copy', '-c:a', 'aac', '-map', '0:v', '-map', '1:a',
                        '-shortest', '-y', final_output_path
                    ]
                print(f"Running FFmpeg music command: {' '.join(music_cmd)}")
                music_result = subprocess.run(music_cmd, capture_output=True, text=True)
                
                if music_result.returncode == 0:
                    print("Background music added successfully!")
                    # Clean up temp music file only if it was downloaded from YouTube
                    if music_source == 'youtube' and os.path.exists(background_music_path):
                        os.remove(background_music_path)
                else:
                    print(f"Failed to add background music: {music_result.stderr}")
                    # Fall back to video without music
                    shutil.copy2(temp_output_path, final_output_path)

            else:
                # Keep original audio or no audio
                if audio_option == 'original':
                    print("Keeping original audio")
                    shutil.copy2(temp_output_path, final_output_path)
                else:
                    print("Removing audio")
                    no_audio_cmd = [
                        'ffmpeg', '-i', temp_output_path, '-c:v', 'copy', '-an',
                        '-y', final_output_path
                    ]
                    print(f"Running FFmpeg no-audio command: {' '.join(no_audio_cmd)}")
                    no_audio_result = subprocess.run(no_audio_cmd, capture_output=True, text=True)
                    
                    if no_audio_result.returncode != 0:
                        print(f"Failed to remove audio: {no_audio_result.stderr}")
                        # Fall back to video with audio
                        shutil.copy2(temp_output_path, final_output_path)
            
            # Clean up temp files
            for temp_file in valid_clip_files:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            if os.path.exists(concat_list_path):
                os.remove(concat_list_path)
            if os.path.exists(temp_output_path):
                os.remove(temp_output_path)
            
            # Update status
            processing_status[process_id].update({
                'status': 'completed',
                'progress': 100,
                'message': 'Auto-processing completed successfully using FFmpeg',
                'output_path': final_output_path
            })
            
            # Update batch status
            if batch_id in processing_status:
                processing_status[batch_id].update({
                    'status': 'completed',
                    'progress': 100,
                    'message': 'All videos processed successfully using FFmpeg!',
                    'final_video_path': final_output_path
                })
            
            # Keep downloaded videos for reuse - no automatic cleanup
            print("Processing completed successfully. Downloaded videos are kept for reuse.")
            print(f"Currently tracking {len(downloaded_videos)} downloaded videos")
            
            return
            
        else:
            print("Using single clip file")
            # If only one clip, add fade effects and copy to output
            temp_single_with_fades = os.path.join(app.config['TEMP_FOLDER'], f'temp_single_with_fades_{process_id}.mp4')
            output_path = os.path.join(app.config['PROCESSED_FOLDER'], f'auto_combined_{process_id}.mp4')
            
            # Create intro screen if enabled (for single clip)
            batch_status = processing_status.get(batch_id, {})
            add_intro_screen = batch_status.get('add_intro_screen', False)
            intro_screen_text = batch_status.get('intro_screen_text', '')
            
            print(f"DEBUG: Single clip intro screen check - add_intro_screen: {add_intro_screen}, intro_screen_text: '{intro_screen_text}'")
            print(f"DEBUG: Single clip full batch status keys: {list(batch_status.keys())}")
            
            if add_intro_screen and intro_screen_text:
                print(f"Creating intro screen for single clip with text: '{intro_screen_text}'")
                intro_screen_path = os.path.join(app.config['TEMP_FOLDER'], f'intro_screen_single_{process_id}.mp4')
                
                # Create a 3-second black video with white text using a more reliable approach
                # First create a simple black video
                black_video_cmd = [
                    'ffmpeg', '-f', 'lavfi', '-i', 'color=black:size=1920x1080:duration=3',
                    '-c:v', 'libx264', '-y', intro_screen_path
                ]
                
                print(f"Running FFmpeg black video command for single clip: {' '.join(black_video_cmd)}")
                black_result = subprocess.run(black_video_cmd, capture_output=True, text=True)
                
                if black_result.returncode == 0:
                    print("Black video created successfully for single clip!")
                    
                    # Now add text overlay to the black video
                    text_overlay_cmd = [
                        'ffmpeg', '-i', intro_screen_path,
                        '-vf', f'drawtext=text=\'{intro_screen_text}\':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2',
                        '-c:v', 'libx264', '-c:a', 'aac', '-y', os.path.join(app.config['TEMP_FOLDER'], f'intro_with_text_single_{process_id}.mp4')
                    ]
                    
                    print(f"Running FFmpeg text overlay command for single clip: {' '.join(text_overlay_cmd)}")
                    text_result = subprocess.run(text_overlay_cmd, capture_output=True, text=True)
                    
                    if text_result.returncode == 0:
                        print("Text overlay added successfully for single clip!")
                        # Update intro screen path to the one with text
                        intro_screen_path = os.path.join(app.config['TEMP_FOLDER'], f'intro_with_text_single_{process_id}.mp4')
                        
                        # Concatenate intro screen with the single clip
                        temp_with_intro = os.path.join(app.config['TEMP_FOLDER'], f'temp_single_with_intro_{process_id}.mp4')
                        intro_concat_cmd = [
                            'ffmpeg', '-i', intro_screen_path, '-i', valid_clip_files[0],
                            '-filter_complex', '[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]',
                            '-map', '[outv]', '-map', '[outa]',
                            '-c:v', 'libx264', '-c:a', 'aac', '-y', temp_with_intro
                        ]
                        
                        print(f"Running FFmpeg intro concatenation for single clip: {' '.join(intro_concat_cmd)}")
                        intro_concat_result = subprocess.run(intro_concat_cmd, capture_output=True, text=True)
                        
                        if intro_concat_result.returncode == 0:
                            print("Intro screen concatenated successfully with single clip!")
                            # Use the clip with intro for further processing
                            valid_clip_files[0] = temp_with_intro
                            # Clean up the separate intro file
                            if os.path.exists(intro_screen_path):
                                os.remove(intro_screen_path)
                        else:
                            print(f"Failed to concatenate intro screen with single clip: {intro_concat_result.stderr}")
                            print("Continuing without intro screen...")
                    else:
                        print(f"Failed to add text overlay for single clip: {text_result.stderr}")
                        print("Continuing without intro screen...")
                else:
                    print(f"Failed to create black video for single clip: {black_result.stderr}")
                    print("Continuing without intro screen...")
            else:
                print("Intro screen disabled or no text provided for single clip, skipping...")
            
            # Add fade in/out effects to single clip (if enabled)
            add_fade_effects = batch_status.get('add_fade_effects', True)
            
            if add_fade_effects:
                print("Adding fade in/out effects to single clip...")
                
                # Get video duration for fade calculations
                duration_cmd = [
                    'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1', valid_clip_files[0]
                ]
                try:
                    duration_result = subprocess.run(duration_cmd, capture_output=True, text=True)
                    if duration_result.returncode == 0:
                        video_duration = float(duration_result.stdout.strip())
                        print(f"Single clip duration for fade effects: {video_duration:.2f}s")
                        
                        # Add 1 second fade in at start and 1 second fade out at end
                        fade_cmd = [
                            'ffmpeg', '-i', valid_clip_files[0],
                            '-vf', f'fade=t=in:st=0:d=1,fade=t=out:st={video_duration-1}:d=1',
                            '-c:v', 'libx264', '-c:a', 'aac', '-y', temp_single_with_fades
                        ]
                        
                        print(f"Running FFmpeg fade command for single clip: {' '.join(fade_cmd)}")
                        fade_result = subprocess.run(fade_cmd, capture_output=True, text=True)
                        
                        if fade_result.returncode == 0:
                            print("Fade effects added successfully to single clip!")
                            # Copy the video with fades to the final output
                            shutil.copy2(temp_single_with_fades, output_path)
                            # Clean up temp fade file
                            if os.path.exists(temp_single_with_fades):
                                os.remove(temp_single_with_fades)
                        else:
                            print(f"Failed to add fade effects to single clip: {fade_result.stderr}")
                            print("Copying single clip without fade effects...")
                            shutil.copy2(valid_clip_files[0], output_path)
                    else:
                        print("Could not get single clip duration for fade effects, copying without fades...")
                        shutil.copy2(valid_clip_files[0], output_path)
                except Exception as e:
                    print(f"Error adding fade effects to single clip: {e}, copying without fades...")
                    shutil.copy2(valid_clip_files[0], output_path)
            else:
                print("Fade effects disabled for single clip, copying without fades...")
                shutil.copy2(valid_clip_files[0], output_path)
            
            print(f"Single clip processed to: {output_path}")
            
            # Clean up temp file
            if os.path.exists(valid_clip_files[0]):
                os.remove(valid_clip_files[0])
            
            # Handle audio options for single clip
            # Get music source and file from batch status
            batch_status = processing_status.get(batch_id, {})
            music_source = batch_status.get('music_source', 'youtube')
            local_music_file = batch_status.get('local_music_file', '')
            
            print(f"DEBUG: Single clip - Processing audio option: '{audio_option}' with music source: '{music_source}'")
            print(f"DEBUG: Background music URL: '{background_music_url}', Local music file: '{local_music_file}'")
            
            if audio_option == 'background' and (background_music_url or local_music_file):
                print("Replacing original audio with background music for single clip...")
                
                # Get background music path based on source
                background_music_path = None
                random_start_time = 0  # Default for YouTube music
                
                if music_source == 'youtube' and background_music_url:
                    # Download background music from YouTube
                    background_music_path = download_background_music(background_music_url, process_id)
                    if not background_music_path:
                        print("Failed to download background music from YouTube for single clip")
                        return
                elif music_source == 'local' and local_music_file:
                    # Use local music file
                    music_folder = app.config['MUSIC_FOLDER']
                    background_music_path = os.path.join(music_folder, local_music_file)
                    if not os.path.exists(background_music_path):
                        print(f"Local music file not found for single clip: {background_music_path}")
                        return
                    
                    # Get music file duration and calculate random start time
                    music_duration_cmd = [
                        'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1', background_music_path
                    ]
                    try:
                        duration_result = subprocess.run(music_duration_cmd, capture_output=True, text=True)
                        if duration_result.returncode == 0:
                            music_duration = float(duration_result.stdout.strip())
                            # Get video duration to determine how much music we need
                            video_duration_cmd = [
                                'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                                '-of', 'default=noprint_wrappers=1:nokey=1', output_path
                            ]
                            video_duration_result = subprocess.run(video_duration_cmd, capture_output=True, text=True)
                            if video_duration_result.returncode == 0:
                                video_duration = float(video_duration_result.stdout.strip())
                                
                                # Calculate random start time (leave some buffer at the end)
                                max_start_time = max(0, music_duration - video_duration - 10)  # 10 second buffer
                                if max_start_time > 0:
                                    import random
                                    random_start_time = random.uniform(0, max_start_time)
                                    print(f"Using local music file for single clip: {background_music_path}")
                                    print(f"Music duration: {music_duration:.2f}s, Video duration: {video_duration:.2f}s")
                                    print(f"Starting music at random time: {random_start_time:.2f}s")
                                else:
                                    random_start_time = 0
                                    print(f"Using local music file for single clip: {background_music_path} (from beginning)")
                            else:
                                random_start_time = 0
                                print(f"Using local music file for single clip: {background_music_path} (could not get video duration)")
                        else:
                            random_start_time = 0
                            print(f"Using local music file for single clip: {background_music_path} (could not get music duration)")
                    except Exception as e:
                        random_start_time = 0
                        print(f"Using local music file for single clip: {background_music_path} (error getting duration: {e})")
                else:
                    print("No valid music source found for single clip")
                    return
                
                # Replace original audio with background music
                if random_start_time > 0:
                    # Use random start time for local music
                    music_cmd = [
                        'ffmpeg', '-i', output_path, '-ss', str(random_start_time), '-i', background_music_path,
                        '-c:v', 'copy', '-c:a', 'aac', '-map', '0:v', '-map', '1:a',
                        '-shortest', '-y', os.path.join(app.config['TEMP_FOLDER'], f'temp_single_with_music_{process_id}.mp4')
                    ]
                else:
                    # Use from beginning (YouTube music or local music fallback)
                    music_cmd = [
                        'ffmpeg', '-i', output_path, '-i', background_music_path,
                        '-c:v', 'copy', '-c:a', 'aac', '-map', '0:v', '-map', '1:a',
                        '-shortest', '-y', os.path.join(app.config['TEMP_FOLDER'], f'temp_single_with_music_{process_id}.mp4')
                    ]
                print(f"Running FFmpeg music command for single clip: {' '.join(music_cmd)}")
                music_result = subprocess.run(music_cmd, capture_output=True, text=True)
                
                if music_result.returncode == 0:
                    print("Background music added successfully to single clip!")
                    # Replace the output with the music version
                    temp_music_path = os.path.join(app.config['TEMP_FOLDER'], f'temp_single_with_music_{process_id}.mp4')
                    shutil.move(temp_music_path, output_path)
                    # Clean up temp music file only if it was downloaded from YouTube
                    if music_source == 'youtube' and os.path.exists(background_music_path):
                        os.remove(background_music_path)
                else:
                    print(f"Failed to add background music to single clip: {music_result.stderr}")

            elif audio_option == 'remove':
                print("Removing audio from single clip")
                no_audio_cmd = [
                    'ffmpeg', '-i', output_path, '-c:v', 'copy', '-an',
                    '-y', os.path.join(app.config['TEMP_FOLDER'], f'temp_single_no_audio_{process_id}.mp4')
                ]
                print(f"Running FFmpeg no-audio command for single clip: {' '.join(no_audio_cmd)}")
                no_audio_result = subprocess.run(no_audio_cmd, capture_output=True, text=True)
                
                if no_audio_result.returncode == 0:
                    print("Audio removed successfully from single clip!")
                    # Replace the output with the no-audio version
                    temp_no_audio_path = os.path.join(app.config['TEMP_FOLDER'], f'temp_single_no_audio_{process_id}.mp4')
                    shutil.move(temp_no_audio_path, output_path)
                else:
                    print(f"Failed to remove audio from single clip: {no_audio_result.stderr}")
            else:
                print("Keeping original audio for single clip")
            
            # Update status and return early
            processing_status[process_id].update({
                'status': 'completed',
                'progress': 100,
                'message': 'Auto-processing completed successfully using FFmpeg',
                'output_path': output_path
            })
            
            # Update batch status
            if batch_id in processing_status:
                processing_status[batch_id].update({
                    'status': 'completed',
                    'progress': 100,
                    'message': 'All videos processed successfully using FFmpeg!',
                    'final_video_path': output_path
                })
            
            # Keep downloaded videos for reuse - no automatic cleanup
            print("Single clip processing completed successfully. Downloaded videos are kept for reuse.")
            print(f"Currently tracking {len(downloaded_videos)} downloaded videos")
            
            return
        
        # Validate final video
        if final_video is None:
            raise ValueError("Final video is None after concatenation")
        if not hasattr(final_video, 'duration'):
            raise ValueError("Final video has no duration attribute")
        
        print(f"Final video validated. Duration: {final_video.duration}s")
        
        # This code should never be reached since FFmpeg handles everything above
        print("ERROR: This code should never be reached!")
        raise Exception("Unexpected code path reached - FFmpeg should have handled everything")
        
    except Exception as e:
        print(f"Auto processing error: {str(e)}")
        processing_status[process_id].update({
            'status': 'error',
            'message': f'Auto-processing failed: {str(e)}'
        })
        
        # Update batch status
        if batch_id in processing_status:
            processing_status[batch_id].update({
                'status': 'error',
                'message': f'Auto-processing failed: {str(e)}'
            })

def process_video_thread(video_path, cuts, background_music, process_id):
    try:
        processing_status[process_id]['message'] = 'Loading video...'
        processing_status[process_id]['progress'] = 10
        
        # Load video
        video = VideoFileClip(video_path)
        
        processing_status[process_id]['message'] = 'Processing video cuts...'
        processing_status[process_id]['progress'] = 30
        
        # Process cuts
        clips = []
        if cuts:
            for i, (start, end) in enumerate(cuts):
                if start < end and end <= video.duration:
                    clip = video.subclipped(start, end)
                    clips.append(clip)
                    processing_status[process_id]['progress'] = 30 + (i + 1) * 20 // len(cuts)
        else:
            # If no cuts specified, use entire video
            clips = [video]
        
        processing_status[process_id]['message'] = 'Concatenating clips...'
        processing_status[process_id]['progress'] = 60
        
        # Concatenate clips
        if len(clips) > 1:
            final_video = concatenate_videoclips(clips)
        else:
            final_video = clips[0]
        
        processing_status[process_id]['message'] = 'Adding background music...'
        processing_status[process_id]['progress'] = 80
        
        # Add background music if provided
        if background_music and os.path.exists(background_music):
            try:
                audio_clip = AudioFileClip(background_music)
                # Loop audio if it's shorter than video
                if audio_clip.duration < final_video.duration:
                    loops_needed = int(final_video.duration / audio_clip.duration) + 1
                    audio_clip = concatenate_videoclips([audio_clip] * loops_needed)
                
                # Trim to video length
                audio_clip = audio_clip.subclip(0, final_video.duration)
                
                # Combine with original audio (if any) or replace
                if final_video.audio:
                    final_audio = CompositeAudioClip([final_video.audio, audio_clip.volumex(app.config['BACKGROUND_MUSIC_VOLUME'])])
                else:
                    final_audio = audio_clip
                
                final_video = final_video.set_audio(final_audio)
            except Exception as e:
                processing_status[process_id]['message'] = f'Warning: Could not add background music: {str(e)}'
        
        processing_status[process_id]['message'] = 'Rendering final video...'
        processing_status[process_id]['progress'] = 90
        
        # Save final video
        output_path = os.path.join(app.config['PROCESSED_FOLDER'], f'final_{process_id}.mp4')
        final_video.write_videofile(output_path, codec=app.config['VIDEO_CODEC'], audio_codec=app.config['AUDIO_CODEC'])
        
        # Clean up
        video.close()
        final_video.close()
        if 'audio_clip' in locals():
            audio_clip.close()
        
        processing_status[process_id].update({
            'status': 'completed',
            'progress': 100,
            'message': 'Processing completed',
            'output_path': output_path
        })
        
    except Exception as e:
        processing_status[process_id].update({
            'status': 'error',
            'message': f'Processing failed: {str(e)}'
        })

@app.route('/upload_music', methods=['POST'])
def upload_music():
    try:
        if 'music_file' not in request.files:
            return jsonify({'error': 'No file uploaded'}), 400
        
        file = request.files['music_file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        if file:
            filename = secure_filename(file.filename)
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(file_path)
            
            return jsonify({
                'message': 'Music file uploaded successfully',
                'file_path': file_path
            })
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download_result/<process_id>')
def download_result(process_id):
    try:
        # Check if it's a batch ID
        if process_id.startswith('batch_'):
            if process_id not in processing_status:
                return jsonify({'error': 'Batch not found'}), 404
            
            status = processing_status[process_id]
            if status['status'] != 'completed':
                return jsonify({'error': 'Batch processing not completed'}), 400
            
            output_path = status.get('final_video_path')
            if not output_path or not os.path.exists(output_path):
                return jsonify({'error': 'Output file not found'}), 404
            
            # Keep downloaded videos for reuse - no cleanup on download
            print("Download started. Downloaded videos are kept for reuse.")
            
            return send_file(output_path, as_attachment=True, download_name=f'combined_video_{process_id}.mp4')
        
        # Check if it's a process ID
        else:
            if process_id not in processing_status:
                return jsonify({'error': 'Process not found'}), 404
            
            status = processing_status[process_id]
            if status['status'] != 'completed':
                return jsonify({'error': 'Processing not completed'}), 400
            
            output_path = status.get('output_path')
            if not output_path or not os.path.exists(output_path):
                return jsonify({'error': 'Output file not found'}), 404
            
            # Keep downloaded videos for reuse - no cleanup on download
            print("Download started. Downloaded videos are kept for reuse.")
            
            return send_file(output_path, as_attachment=True, download_name=f'final_video_{process_id}.mp4')
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test_processing', methods=['POST'])
def test_processing():
    """Test endpoint to verify video processing is working"""
    try:
        # Check if we have any existing video files to test with
        existing_videos = [f for f in os.listdir(app.config['TEMP_FOLDER']) if f.endswith(('.mp4', '.avi', '.mov', '.mkv'))]
        
        if existing_videos:
            # Use the first existing video for testing
            test_video_path = os.path.join(app.config['TEMP_FOLDER'], existing_videos[0])
            print(f"Testing with existing video: {test_video_path}")
            
            # Test basic video loading
            try:
                video = VideoFileClip(test_video_path)
                duration = video.duration
                video.close()
                
                return jsonify({
                    'message': 'Video loading test successful',
                    'test_video': test_video_path,
                    'duration': duration,
                    'file_size': os.path.getsize(test_video_path)
                })
            except Exception as e:
                return jsonify({'error': f'Video loading failed: {str(e)}'}), 500
        else:
            return jsonify({'error': 'No video files found for testing'}), 400
        
    except Exception as e:
        return jsonify({'error': f'Test processing failed: {str(e)}'}), 500

def download_background_music(youtube_url, process_id):
    """Download background music from YouTube URL"""
    try:
        print(f"Downloading background music from: {youtube_url}")
        
        # Extract video ID
        if 'youtube.com/watch?v=' in youtube_url:
            video_id = youtube_url.split('v=')[1].split('&')[0]
        elif 'youtu.be/' in youtube_url:
            video_id = youtube_url.split('youtu.be/')[1].split('?')[0]
        else:
            print(f"Invalid YouTube URL: {youtube_url}")
            return None
        
        # Download audio only using yt-dlp
        output_path = os.path.join(app.config['TEMP_FOLDER'], f'background_music_{process_id}.mp3')
        
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_path.replace('.mp3', '.%(ext)s'),
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            'quiet': True,
            'no_warnings': True
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])
        
        # Find the downloaded file
        downloaded_files = [f for f in os.listdir(app.config['TEMP_FOLDER']) if f.startswith(f'background_music_{process_id}')]
        
        if downloaded_files:
            music_path = os.path.join(app.config['TEMP_FOLDER'], downloaded_files[0])
            if os.path.exists(music_path) and os.path.getsize(music_path) > 0:
                print(f"Background music downloaded successfully: {music_path}")
                return music_path
            else:
                print(f"Background music file is invalid: {music_path}")
                return None
        else:
            print("No background music file found after download")
            return None
            
    except Exception as e:
        print(f"Error downloading background music: {e}")
        return None

OPENXBL_BASE_URL = 'https://xbl.io/api/v2'
GAMES_CACHE_TTL = 2 * 3600  # Reuse a gamertag's saved games if it was last used within 2 hours (OpenXBL allows 150 requests/hour)
GAMES_CACHE_FILE = os.path.join(DATA_FOLDER, 'cache', 'gamertag_cache.json')
MAX_GAMES_RETURNED = 50

# The cache file maps lowercase gamertag -> {'gamertag', 'games', 'fetched_at', 'last_used'}.
# It lives on disk so it survives restarts and is shared between gunicorn workers.
_games_cache_lock = threading.Lock()


def load_games_cache():
    try:
        with open(GAMES_CACHE_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_games_cache(cache):
    """Drop gamertags not used within the TTL, then write the cache file atomically"""
    now = time.time()
    cache = {k: v for k, v in cache.items() if now - v.get('last_used', 0) < GAMES_CACHE_TTL}
    os.makedirs(os.path.dirname(GAMES_CACHE_FILE), exist_ok=True)
    temp_path = f"{GAMES_CACHE_FILE}.{os.getpid()}.tmp"
    with open(temp_path, 'w') as f:
        json.dump(cache, f)
    os.replace(temp_path, GAMES_CACHE_FILE)


class OpenXBLError(Exception):
    """Error from the OpenXBL API with an HTTP status to return to the client"""
    def __init__(self, message, status_code=500):
        super().__init__(message)
        self.status_code = status_code


def openxbl_get(path):
    """GET an OpenXBL endpoint and return the 'content' of the JSON response"""
    api_key = os.environ.get('OPENXBL_API_KEY')
    if not api_key:
        raise OpenXBLError('OPENXBL_API_KEY is not configured on the server', 500)

    response = requests.get(
        f"{OPENXBL_BASE_URL}{path}",
        headers={'X-Authorization': api_key, 'Accept': 'application/json'},
        timeout=30
    )
    remaining = response.headers.get('x-ratelimit-remaining')
    if remaining is not None:
        print(f"OpenXBL rate limit remaining: {remaining}")

    if response.status_code == 429:
        raise OpenXBLError('Xbox lookup limit reached for this hour. Please try again later.', 429)
    if response.status_code in (401, 403):
        raise OpenXBLError('OpenXBL API key was rejected. Check OPENXBL_API_KEY.', 500)
    response.raise_for_status()

    data = response.json()
    return data.get('content', data)


def clean_game_title(name):
    """Strip trademark symbols so titles work well in overlays and YouTube searches"""
    name = re.sub(r'[™®©]', '', name)
    return ' '.join(name.split())


def get_xbox_games(gamertag):
    """Return the most recently played games for a gamertag. A saved list is reused, without
    calling OpenXBL, if the gamertag was last used within GAMES_CACHE_TTL"""
    cache_key = gamertag.lower()
    with _games_cache_lock:
        cache = load_games_cache()
        entry = cache.get(cache_key)
        if entry and time.time() - entry.get('last_used', 0) < GAMES_CACHE_TTL:
            entry['last_used'] = time.time()
            save_games_cache(cache)
            print(f"Using saved games for {gamertag} (fetched {int((time.time() - entry['fetched_at']) / 60)} min ago)")
            return entry['games']

    # Look up the gamertag to get the player's XUID
    people = openxbl_get(f"/search/{requests.utils.quote(gamertag)}").get('people') or []
    player = next((p for p in people if (p.get('gamertag') or '').lower() == cache_key), None)
    if not player and people:
        player = people[0]
    if not player or not player.get('xuid'):
        raise OpenXBLError(f'Xbox gamertag "{gamertag}" was not found', 404)

    # Title history comes back sorted by most recently played
    titles = openxbl_get(f"/player/titleHistory/{player['xuid']}").get('titles') or []
    if not titles:
        raise OpenXBLError(f'No games found for "{gamertag}". Their game history may be set to private on Xbox.', 404)

    games = []
    seen = set()
    for title in titles:
        if title.get('type') != 'Game':
            continue
        name = clean_game_title(title.get('name') or '')
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        games.append({
            'title': name,
            'image': title.get('displayImage'),
            'last_played': (title.get('titleHistory') or {}).get('lastTimePlayed')
        })
        if len(games) >= MAX_GAMES_RETURNED:
            break

    with _games_cache_lock:
        cache = load_games_cache()
        now = time.time()
        cache[cache_key] = {'gamertag': gamertag, 'games': games, 'fetched_at': now, 'last_used': now}
        save_games_cache(cache)
    return games


@app.route('/scrape_trueachievements', methods=['POST'])
@limiter.limit("10 per hour", error_message="You can look up 10 gamertags per hour.")
def scrape_trueachievements():
    """Fetch a gamer's recently played games from Xbox Live via OpenXBL.
    Keeps its original route name so existing frontend calls keep working."""
    try:
        data = request.get_json()
        user_id = (data.get('user_id') or '').strip()

        if not user_id:
            return jsonify({'success': False, 'error': 'Xbox gamertag is required'}), 400

        print(f"Fetching Xbox games for gamertag: {user_id}")
        games = get_xbox_games(user_id)
        print(f"Found {len(games)} games")

        return jsonify({
            'success': True,
            'games': games,
            'total_found': len(games)
        })

    except OpenXBLError as e:
        print(f"OpenXBL error: {e}")
        return jsonify({'success': False, 'error': str(e)}), e.status_code
    except requests.exceptions.RequestException as e:
        print(f"Request error: {e}")
        return jsonify({'success': False, 'error': f'Failed to reach Xbox Live: {str(e)}'}), 502
    except Exception as e:
        print(f"Game lookup error: {e}")
        return jsonify({'success': False, 'error': f'Game lookup failed: {str(e)}'}), 500

YOUTUBE_SEARCH_RESULTS = 8  # How many search results to consider per query
YOUTUBE_PREFERRED_MIN_DURATION = 300  # Prefer videos at least 5 minutes long

# Videos with these in the title are talking, not gameplay (interviews, podcasts, news, rankings...)
YOUTUBE_EXCLUDED_TITLE_PATTERN = re.compile(
    r"\b(interviews?|podcast|reactions?|reacts?|reacting|news|panel|tier list|top \d+|"
    r"livestreams?|live stream|comparison|vs\.?|unboxing|explained|theories|theory|lore)\b",
    re.IGNORECASE
)
# Small words that don't need to appear in a video title for it to match a game name
TITLE_MATCH_IGNORED_WORDS = {'the', 'of', 'a', 'an', 'and'}


def title_words(text):
    """Lowercase words in a title, ignoring punctuation and trademark symbols"""
    return re.sub(r'[^a-z0-9]+', ' ', text.lower()).split()


def title_matches_game(video_title, game_name):
    """True if every significant word of the game name appears in the video title"""
    video_words = set(title_words(video_title))
    game_words = [w for w in title_words(game_name) if w not in TITLE_MATCH_IGNORED_WORDS]
    return all(w in video_words for w in game_words)

@app.route('/search_youtube', methods=['POST'])
@limiter.limit("60 per hour", error_message="You've reached the limit of 60 YouTube searches per hour.")
def search_youtube():
    try:
        data = request.get_json()
        query = data.get('query')
        max_duration = data.get('max_duration', 600)  # Default to 10 minutes
        min_duration = data.get('min_duration', 0)  # Default to no minimum
        preferred_min_duration = data.get('preferred_min_duration', YOUTUBE_PREFERRED_MIN_DURATION)
        game_name = (data.get('game_name') or '').strip()  # When given, video titles must mention the game
        
        if not query:
            return jsonify({'success': False, 'error': 'Search query is required'}), 400
        
        duration_range = f"{min_duration//60}-{max_duration//60} minutes" if min_duration > 0 else f"up to {max_duration//60} minutes"
        print(f"Searching YouTube for: '{query}' (duration: {duration_range})")
        
        # Use yt-dlp to search YouTube
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,  # Don't download, just extract info
            'default_search': 'ytsearch',  # Search YouTube
            'playlist_items': f'1:{YOUTUBE_SEARCH_RESULTS}',
            'format': 'best[height<=720]',  # Prefer 720p or lower for montages
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # Search for the query
                search_results = ydl.extract_info(f"ytsearch{YOUTUBE_SEARCH_RESULTS}:{query}", download=False)
                
                if not search_results or 'entries' not in search_results or not search_results['entries']:
                    return jsonify({'success': False, 'error': f'No videos found for: {query}'}), 404
                
                # Get all valid results
                valid_videos = []
                for video_info in search_results['entries']:
                    if not video_info:
                        continue
                        
                    # Check if video duration is within limits
                    duration = video_info.get('duration') or 0  # None for live streams
                    if duration < min_duration:
                        print(f"Video duration {duration}s is below minimum {min_duration}s for: {query}")
                        continue
                    elif duration > max_duration:
                        print(f"Video duration {duration}s exceeds limit {max_duration}s for: {query}")
                        continue
                    
                    # Extract video details
                    video_url = video_info.get('url', '')
                    video_title = video_info.get('title') or 'Unknown'
                    video_duration = duration

                    # Skip videos about other games and talking-heads content
                    if game_name and not title_matches_game(video_title, game_name):
                        print(f"Skipping video not titled with '{game_name}': {video_title}")
                        continue
                    if YOUTUBE_EXCLUDED_TITLE_PATTERN.search(video_title):
                        print(f"Skipping non-gameplay video: {video_title}")
                        continue

                    if video_url:
                        valid_videos.append({
                            'video_url': video_url,
                            'video_title': video_title,
                            'video_duration': video_duration
                        })
                        print(f"Found video: {video_title} ({video_duration}s) - {video_url}")
                
                if not valid_videos:
                    return jsonify({'success': False, 'error': f'No suitable videos found for: {query}'}), 404

                # Longer videos give the clip segments more room to spread out, so rank videos at
                # least preferred_min_duration long first (keeping YouTube's relevance order),
                # then shorter ones longest first. A preferred_min_duration of 0 keeps relevance order.
                long_videos = [v for v in valid_videos if v['video_duration'] >= preferred_min_duration]
                short_videos = sorted(
                    (v for v in valid_videos if v['video_duration'] < preferred_min_duration),
                    key=lambda v: v['video_duration'], reverse=True
                )
                valid_videos = (long_videos + short_videos)[:3]

                # Return the first valid video (for backward compatibility)
                first_video = valid_videos[0]
                return jsonify({
                    'success': True,
                    'video_url': first_video['video_url'],
                    'video_title': first_video['video_title'],
                    'video_duration': first_video['video_duration'],
                    'search_query': query,
                    'all_videos': valid_videos  # Include all valid videos
                })
                
        except Exception as e:
            print(f"YouTube search error for '{query}': {e}")
            return jsonify({'success': False, 'error': f'YouTube search failed: {str(e)}'}), 500
        
    except Exception as e:
        print(f"Search endpoint error: {e}")
        return jsonify({'success': False, 'error': f'Search failed: {str(e)}'}), 500

@app.route('/stop_process/<batch_id>', methods=['POST'])
def stop_process(batch_id):
    try:
        if batch_id not in processing_status:
            return jsonify({'success': False, 'error': 'Batch not found'}), 404
        
        # Mark the batch as stopped
        processing_status[batch_id].update({
            'status': 'stopped',
            'message': 'Process stopped by user'
        })
        
        # Clean up any temporary files
        try:
            if os.path.exists(app.config['TEMP_FOLDER']):
                for file in os.listdir(app.config['TEMP_FOLDER']):
                    if file.startswith(batch_id):
                        file_path = os.path.join(app.config['TEMP_FOLDER'], file)
                        try:
                            if os.path.isfile(file_path):
                                os.remove(file_path)
                            elif os.path.isdir(file_path):
                                shutil.rmtree(file_path)
                        except Exception as e:
                            print(f"Could not remove temp file {file_path}: {e}")
        except Exception as e:
            print(f"Error during cleanup: {e}")
        
        return jsonify({'success': True, 'message': 'Process stopped successfully'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/get_music_files')
def get_music_files():
    """Get list of music files from the music folder"""
    try:
        music_folder = app.config['MUSIC_FOLDER']
        
        # Create music folder if it doesn't exist
        if not os.path.exists(music_folder):
            os.makedirs(music_folder, exist_ok=True)
            return jsonify({
                'success': True,
                'music_files': [],
                'message': 'Music folder created. Add music files to get started.'
            })
        
        # Get list of music files
        music_files = []
        supported_formats = ['.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac']
        
        for filename in os.listdir(music_folder):
            filename_lower = filename.lower()
            
            # Check if file ends with any supported format
            if any(filename_lower.endswith(fmt) for fmt in supported_formats):
                file_path = os.path.join(music_folder, filename)
                file_size = os.path.getsize(file_path)
                
                # Get duration using ffprobe if possible
                duration = None
                try:
                    duration_cmd = [
                        'ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
                        '-of', 'default=noprint_wrappers=1:nokey=1', file_path
                    ]
                    result = subprocess.run(duration_cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        duration_seconds = float(result.stdout.strip())
                        duration = f"{int(duration_seconds // 60)}:{int(duration_seconds % 60):02d}"
                except Exception as e:
                    pass
                
                music_files.append({
                    'filename': filename,
                    'size': file_size,
                    'duration': duration,
                    'path': file_path
                })
        
        # Sort by filename
        music_files.sort(key=lambda x: x['filename'].lower())
        
        return jsonify({
            'success': True,
            'music_files': music_files,
            'total_files': len(music_files)
        })
        
    except Exception as e:
        print(f"Error getting music files: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/music/<filename>')
def serve_music(filename):
    """Serve music files for preview"""
    try:
        music_folder = app.config['MUSIC_FOLDER']
        file_path = os.path.join(music_folder, filename)
        
        if not os.path.exists(file_path):
            return jsonify({'error': 'Music file not found'}), 404
        
        # Check if file is a supported music format
        supported_formats = ['.mp3', '.wav', '.m4a', '.aac', '.ogg', '.flac']
        if not any(filename.lower().endswith(fmt) for fmt in supported_formats):
            return jsonify({'error': 'Unsupported file format'}), 400
        
        return send_file(file_path, mimetype='audio/mpeg')
        
    except Exception as e:
        print(f"Error serving music file: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/cleanup', methods=['POST'])
def cleanup():
    try:
        # Only remove old files; other users' jobs may be using the recent ones
        cleanup_old_files()
        
        return jsonify({'message': 'Cleanup completed'})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/cost_status')
def cost_status():
    """Get current cost monitoring status"""
    try:
        status = get_cost_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/cost_check')
def cost_check():
    """Check if cost limits are exceeded and should shutdown"""
    try:
        should_shutdown = check_cost_limits()
        if should_shutdown:
            return jsonify({
                'should_shutdown': True,
                'message': 'Cost limits exceeded. App should be shut down.',
                'status': get_cost_status()
            }), 503  # Service Unavailable
        else:
            return jsonify({
                'should_shutdown': False,
                'message': 'Cost limits OK',
                'status': get_cost_status()
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=app.config['DEBUG'], host=app.config['HOST'], port=app.config['PORT'])

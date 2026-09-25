"""
Configuration file for Video GPT Gaming App
Modify these settings as needed
"""

import os

# Everything the app creates while running (downloads, montages, uploads, cache, logs) goes here
DATA_FOLDER = 'data'

class Config:
    # Flask Configuration
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'your-secret-key-change-this-in-production'
    DEBUG = os.environ.get('FLASK_DEBUG', 'True').lower() == 'true'
    
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # File Upload Configuration
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    UPLOAD_FOLDER = os.path.join(DATA_FOLDER, 'uploads')
    PROCESSED_FOLDER = os.path.join(DATA_FOLDER, 'processed')
    TEMP_FOLDER = os.path.join(DATA_FOLDER, 'temp')
    MUSIC_FOLDER = 'music'  # Background music library, committed to the repo
    
    # Video Processing Configuration
    MAX_VIDEO_HEIGHT = 720  # Maximum video height for processing (pixels)
    SUPPORTED_VIDEO_FORMATS = ['.mp4', '.avi', '.mov', '.mkv', '.webm']
    SUPPORTED_AUDIO_FORMATS = ['.mp3', '.wav', '.aac', '.ogg', '.flac']
    
    # YouTube Download Configuration
    YT_DLP_OPTIONS = {
        'format': 'bestvideo[height<=720]+bestaudio/best[height<=720]',  # Limit to 720p for processing
        'merge_output_format': 'mp4',  # Merge separate video/audio streams into one mp4
        'outtmpl': '%(id)s_%(title)s.%(ext)s',
        'ignoreerrors': False,  # Don't ignore errors for better debugging
        'no_warnings': False,  # Show warnings for debugging
        'noplaylist': True,  # Only download single videos
        'extract_flat': False,  # Extract full video info
        'quiet': False,  # Show progress for debugging
        'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'extractor_retries': 3,  # Retry failed extractions
        'fragment_retries': 3,  # Retry failed fragments
        'retries': 3,  # General retry count
        'sleep_interval': 1,  # Sleep between requests
        'max_sleep_interval': 5,  # Maximum sleep interval
    }
    
    # Processing Configuration
    BACKGROUND_MUSIC_VOLUME = 0.3  # Volume level for background music (0.0 to 1.0)
    VIDEO_CODEC = 'libx264'
    AUDIO_CODEC = 'aac'
    
    # Server Configuration
    HOST = os.environ.get('HOST', '127.0.0.1')
    PORT = int(os.environ.get('PORT', 5001))
    
    # Cleanup Configuration
    AUTO_CLEANUP = True  # Automatically clean up temporary files
    CLEANUP_INTERVAL = 3600  # Cleanup interval in seconds (1 hour)
    
    @staticmethod
    def init_app(app):
        """Initialize application with configuration"""
        # Ensure required directories exist
        for folder in [Config.UPLOAD_FOLDER, Config.PROCESSED_FOLDER, Config.TEMP_FOLDER]:
            os.makedirs(folder, exist_ok=True)

class DevelopmentConfig(Config):
    """Development configuration"""
    DEBUG = True
    AUTO_CLEANUP = False  # Disable auto-cleanup in development

class ProductionConfig(Config):
    """Production configuration"""
    DEBUG = False
    SESSION_COOKIE_SECURE = True  # Only send the login cookie over HTTPS
    SECRET_KEY = os.environ.get('SECRET_KEY') or os.urandom(24)
    AUTO_CLEANUP = True

class TestingConfig(Config):
    """Testing configuration"""
    TESTING = True
    DEBUG = True
    AUTO_CLEANUP = False

# Configuration dictionary
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}

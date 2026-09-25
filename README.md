# Video GPT Gaming App - YouTube Video Processor

A powerful web application that allows you to download YouTube videos, cut them into smaller sections, and edit them together with background music. Built with Flask, yt-dlp, and MoviePy.

## Features

- **YouTube Video Download**: Download videos directly from YouTube URLs
- **Video Cutting**: Cut videos into specific time segments
- **Background Music**: Add custom background music to your videos
- **Video Concatenation**: Combine multiple video cuts into one final video
- **Real-time Progress**: Monitor download and processing progress
- **Modern Web Interface**: Beautiful, responsive design with step-by-step workflow
- **Background Processing**: Non-blocking video processing with status updates

## Prerequisites

- Python 3.7 or higher
- FFmpeg (required for video processing)

### Installing FFmpeg

#### macOS (using Homebrew):
```bash
brew install ffmpeg
```

#### Ubuntu/Debian:
```bash
sudo apt update
sudo apt install ffmpeg
```

#### Windows:
Download from [FFmpeg official website](https://ffmpeg.org/download.html) or use Chocolatey:
```bash
choco install ffmpeg
```

## Installation

1. **Clone or download the project files**

2. **Create a virtual environment (recommended):**
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. **Install dependencies:**
```bash
pip install -r requirements.txt
```

## Usage

1. **Start the application:**
```bash
python app.py
```

2. **Open your web browser and navigate to:**
```
http://localhost:5000
```

3. **Follow the 4-step process:**

   **Step 1: Download Video**
   - Enter a YouTube URL
   - Click "Download" and wait for completion
   
   **Step 2: Define Video Cuts**
   - Add multiple time segments (start and end times in seconds)
   - Click "Process Video" when ready
   
   **Step 3: Background Music (Optional)**
   - Upload an audio file (MP3, WAV, etc.)
   - The music will loop if shorter than the video
   
   **Step 4: Processing**
   - Monitor the processing progress
   - Download the final video when complete

## File Structure

```
Video GPT Gaming App/
├── app.py                 # Main Flask application
├── requirements.txt       # Python dependencies
├── templates/
│   └── index.html        # Web interface
├── uploads/              # Uploaded music files
├── processed/            # Final processed videos
├── temp/                 # Temporary downloaded videos
└── README.md            # This file
```

## API Endpoints

- `GET /` - Main application page
- `POST /download` - Download YouTube video
- `GET /status/<id>` - Get download/processing status
- `POST /process` - Process video with cuts and music
- `POST /upload_music` - Upload background music
- `GET /download_result/<id>` - Download final processed video
- `POST /cleanup` - Clean up temporary files

## Configuration

The application creates several directories automatically:
- `uploads/` - For uploaded music files
- `processed/` - For final output videos
- `temp/` - For temporary downloaded videos

## Troubleshooting

### Common Issues

1. **FFmpeg not found:**
   - Ensure FFmpeg is installed and accessible in your system PATH
   - Restart your terminal after installation

2. **Video download fails:**
   - Check your internet connection
   - Verify the YouTube URL is valid and accessible
   - Some videos may have download restrictions

3. **Processing errors:**
   - Ensure video files are not corrupted
   - Check available disk space
   - Verify audio file formats are supported

4. **Memory issues with large videos:**
   - The app limits downloads to 720p for processing efficiency
   - Consider processing shorter video segments

### Performance Tips

- Use shorter video segments for faster processing
- Compress audio files before upload
- Close other applications during video processing
- Ensure adequate disk space for temporary files

## Dependencies

- **Flask**: Web framework
- **yt-dlp**: YouTube video downloader
- **MoviePy**: Video editing and processing
- **Pillow**: Image processing
- **Werkzeug**: WSGI utilities

## License

This project is open source and available under the MIT License.

## Contributing

Feel free to submit issues, feature requests, or pull requests to improve the application.

## Disclaimer

This application is for educational and personal use only. Please respect YouTube's terms of service and copyright laws when downloading and processing videos. Only download content you have permission to use or that is in the public domain.

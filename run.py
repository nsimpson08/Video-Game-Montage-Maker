#!/usr/bin/env python3
"""
Startup script for Video GPT Gaming App
Checks dependencies and starts the Flask application
"""

import sys
import subprocess
import os

def check_dependency(package_name, import_name=None):
    """Check if a Python package is installed"""
    if import_name is None:
        import_name = package_name
    
    try:
        __import__(import_name)
        print(f"✓ {package_name} is installed")
        return True
    except ImportError:
        print(f"✗ {package_name} is not installed")
        return False

def check_ffmpeg():
    """Check if FFmpeg is available in the system"""
    try:
        result = subprocess.run(['ffmpeg', '-version'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print("✓ FFmpeg is available")
            return True
        else:
            print("✗ FFmpeg is not working properly")
            return False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("✗ FFmpeg is not installed or not in PATH")
        return False

def install_dependencies():
    """Install missing dependencies"""
    print("\nInstalling missing dependencies...")
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt'])
        print("✓ Dependencies installed successfully")
        return True
    except subprocess.CalledProcessError:
        print("✗ Failed to install dependencies")
        return False

def main():
    """Main startup function"""
    print("Video GPT Gaming App - Startup Check")
    print("=" * 40)
    
    # Check Python version
    if sys.version_info < (3, 7):
        print("✗ Python 3.7 or higher is required")
        print(f"Current version: {sys.version}")
        return False
    
    print(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    
    # Check dependencies
    dependencies_ok = True
    dependencies_ok &= check_dependency('flask', 'flask')
    dependencies_ok &= check_dependency('yt_dlp', 'yt_dlp')
    dependencies_ok &= check_dependency('moviepy', 'moviepy')
    dependencies_ok &= check_dependency('PIL', 'PIL')
    
    # Check FFmpeg
    ffmpeg_ok = check_ffmpeg()
    
    if not dependencies_ok:
        print("\nSome Python dependencies are missing.")
        response = input("Would you like to install them now? (y/n): ")
        if response.lower() in ['y', 'yes']:
            if not install_dependencies():
                return False
        else:
            print("Please install the missing dependencies manually:")
            print("pip install -r requirements.txt")
            return False
    
    if not ffmpeg_ok:
        print("\nFFmpeg is required for video processing.")
        print("Please install FFmpeg:")
        print("- macOS: brew install ffmpeg")
        print("- Ubuntu/Debian: sudo apt install ffmpeg")
        print("- Windows: Download from https://ffmpeg.org/download.html")
        return False
    
    print("\n✓ All dependencies are satisfied!")
    print("\nStarting Video GPT Gaming App...")
    print("=" * 40)
    
    # Start the Flask application
    try:
        from app import app
        print("✓ Flask application loaded successfully")
        print("✓ Open your browser and navigate to: http://localhost:5001")
        print("✓ Press Ctrl+C to stop the application")
        print("=" * 40)
        
        app.run(debug=True, host='0.0.0.0', port=5001)
        
    except Exception as e:
        print(f"✗ Failed to start the application: {e}")
        return False
    
    return True

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nApplication stopped by user")
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        sys.exit(1)

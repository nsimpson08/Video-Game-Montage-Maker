#!/usr/bin/env python3
"""
Demo script for Video GPT Gaming App
This script demonstrates how to use the application programmatically
"""

import requests
import time
import json
import os

# Configuration
BASE_URL = "http://localhost:5000"
TEST_YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # Rick Roll - short video for testing

def test_download():
    """Test video download functionality"""
    print("Testing video download...")
    
    # Download video
    response = requests.post(f"{BASE_URL}/download", 
                           json={"url": TEST_YOUTUBE_URL})
    
    if response.status_code == 200:
        data = response.json()
        download_id = data['download_id']
        print(f"✓ Download started with ID: {download_id}")
        
        # Monitor download progress
        while True:
            status_response = requests.get(f"{BASE_URL}/status/{download_id}")
            if status_response.status_code == 200:
                status = status_response.json()
                
                if status['status'] == 'downloading':
                    print(f"Downloading... {status['progress']}% - {status['message']}")
                    time.sleep(2)
                elif status['status'] == 'completed':
                    print(f"✓ Download completed: {status['message']}")
                    return status['video_path']
                elif status['status'] == 'error':
                    print(f"✗ Download failed: {status['message']}")
                    return None
            else:
                print(f"✗ Failed to get status: {status_response.status_code}")
                return None
    else:
        print(f"✗ Download request failed: {response.status_code}")
        return None

def test_processing(video_path, cuts=None):
    """Test video processing functionality"""
    if not video_path:
        print("No video path provided for processing")
        return None
    
    print("\nTesting video processing...")
    
    # Process video
    process_data = {
        "video_path": video_path,
        "cuts": cuts or [[0, 10]],  # Default: first 10 seconds
        "background_music": ""  # No background music for demo
    }
    
    response = requests.post(f"{BASE_URL}/process", json=process_data)
    
    if response.status_code == 200:
        data = response.json()
        process_id = data['process_id']
        print(f"✓ Processing started with ID: {process_id}")
        
        # Monitor processing progress
        while True:
            status_response = requests.get(f"{BASE_URL}/status/{process_id}")
            if status_response.status_code == 200:
                status = status_response.json()
                
                if status['status'] == 'processing':
                    print(f"Processing... {status['progress']}% - {status['message']}")
                    time.sleep(2)
                elif status['status'] == 'completed':
                    print(f"✓ Processing completed: {status['message']}")
                    return process_id
                elif status['status'] == 'error':
                    print(f"✗ Processing failed: {status['message']}")
                    return None
            else:
                print(f"✗ Failed to get processing status: {status_response.status_code}")
                return None
    else:
        print(f"✗ Processing request failed: {response.status_code}")
        return None

def test_music_upload():
    """Test music upload functionality"""
    print("\nTesting music upload...")
    
    # Create a simple test audio file (you can replace this with a real audio file)
    test_audio_path = "test_audio.txt"
    with open(test_audio_path, "w") as f:
        f.write("This is a test file - replace with real audio file")
    
    try:
        with open(test_audio_path, "rb") as f:
            files = {"music_file": f}
            response = requests.post(f"{BASE_URL}/upload_music", files=files)
        
        if response.status_code == 200:
            data = response.json()
            print(f"✓ Music upload successful: {data['message']}")
            return data['file_path']
        else:
            print(f"✗ Music upload failed: {response.status_code}")
            return None
    finally:
        # Clean up test file
        if os.path.exists(test_audio_path):
            os.remove(test_audio_path)

def test_cleanup():
    """Test cleanup functionality"""
    print("\nTesting cleanup...")
    
    response = requests.post(f"{BASE_URL}/cleanup")
    
    if response.status_code == 200:
        data = response.json()
        print(f"✓ Cleanup successful: {data['message']}")
        return True
    else:
        print(f"✗ Cleanup failed: {response.status_code}")
        return False

def main():
    """Main demo function"""
    print("Video GPT Gaming App - Demo Script")
    print("=" * 40)
    
    # Check if server is running
    try:
        response = requests.get(f"{BASE_URL}/")
        if response.status_code != 200:
            print(f"✗ Server not responding properly: {response.status_code}")
            return
        print("✓ Server is running")
    except requests.exceptions.ConnectionError:
        print("✗ Cannot connect to server. Make sure the Flask app is running on http://localhost:5000")
        print("Run: python app.py")
        return
    
    # Test download
    video_path = test_download()
    
    if video_path:
        # Test processing with simple cut
        process_id = test_processing(video_path, [[0, 5]])  # First 5 seconds
        
        if process_id:
            print(f"\n✓ Demo completed successfully!")
            print(f"Process ID: {process_id}")
            print(f"You can download the result at: {BASE_URL}/download_result/{process_id}")
        else:
            print("\n✗ Processing demo failed")
    else:
        print("\n✗ Download demo failed")
    
    # Test music upload
    test_music_upload()
    
    # Test cleanup
    test_cleanup()
    
    print("\n" + "=" * 40)
    print("Demo completed!")

if __name__ == "__main__":
    main()

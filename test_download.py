#!/usr/bin/env python3
"""
Test script for Video GPT Gaming App download functionality
"""

import requests
import time
import json

# Test with a short, single video URL (not a playlist)
# Using a more reliable test video
TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo" - YouTube's first video
BASE_URL = "http://127.0.0.1:5001"

def test_download():
    """Test the download functionality"""
    print("Testing Video GPT Gaming App Download")
    print("=" * 40)
    
    # Check if server is running
    try:
        response = requests.get(f"{BASE_URL}/")
        if response.status_code != 200:
            print(f"❌ Server not responding: {response.status_code}")
            return False
        print("✅ Server is running")
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to server. Make sure it's running on port 5001")
        return False
    
    # Test download
    print(f"\n📥 Testing download with URL: {TEST_URL}")
    
    try:
        response = requests.post(f"{BASE_URL}/download", 
                               json={"url": TEST_URL},
                               timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            download_id = data.get('download_id')
            
            if download_id:
                print(f"✅ Download started with ID: {download_id}")
                print("⏳ Monitoring download progress...")
                
                # Monitor progress
                while True:
                    time.sleep(2)
                    status_response = requests.get(f"{BASE_URL}/status/{download_id}")
                    
                    if status_response.status_code == 200:
                        status = status_response.json()
                        
                        if status['status'] == 'downloading':
                            progress = status.get('progress', 0)
                            message = status.get('message', 'Downloading...')
                            print(f"📊 Progress: {progress}% - {message}")
                            
                        elif status['status'] == 'completed':
                            print(f"✅ Download completed!")
                            print(f"📁 Video path: {status.get('video_path', 'N/A')}")
                            print(f"📝 Title: {status.get('title', 'N/A')}")
                            return True
                            
                        elif status['status'] == 'error':
                            print(f"❌ Download failed: {status.get('message', 'Unknown error')}")
                            return False
                    else:
                        print(f"❌ Failed to get status: {status_response.status_code}")
                        return False
            else:
                print("❌ No download ID received")
                return False
                
        else:
            print(f"❌ Download request failed: {response.status_code}")
            try:
                error_data = response.json()
                print(f"Error details: {error_data}")
            except:
                print(f"Response text: {response.text}")
            return False
            
    except requests.exceptions.Timeout:
        print("❌ Request timed out")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False

def main():
    """Main test function"""
    print("🎬 Video GPT Gaming App - Download Test")
    print("=" * 50)
    
    success = test_download()
    
    if success:
        print("\n🎉 Test completed successfully!")
        print("You can now use the web interface to:")
        print("1. Download videos")
        print("2. Cut them into segments")
        print("3. Add background music")
        print("4. Process and download the final video")
    else:
        print("\n💥 Test failed!")
        print("Check the server logs for more details")
    
    print(f"\n🌐 Open your browser and go to: {BASE_URL}")
    print("Press Ctrl+C to stop the test")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Test stopped by user")
    except Exception as e:
        print(f"\n💥 Unexpected error: {e}")

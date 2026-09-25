#!/usr/bin/env python3
"""
Test script to verify download completion detection
"""

import requests
import time
import json

BASE_URL = "http://127.0.0.1:5001"

def test_download_completion():
    """Test that downloads properly complete and move to next step"""
    print("Testing Download Completion Detection")
    print("=" * 40)
    
    # Check if server is running
    try:
        response = requests.get(f"{BASE_URL}/")
        if response.status_code != 200:
            print(f"❌ Server not responding: {response.status_code}")
            return False
        print("✅ Server is running")
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to server")
        return False
    
    # Test with a very short video
    test_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo" - very short
    
    print(f"\n📥 Testing download completion with: {test_url}")
    
    try:
        # Start download
        response = requests.post(f"{BASE_URL}/download", 
                               json={"url": test_url},
                               timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            download_id = data.get('download_id')
            
            if download_id:
                print(f"✅ Download started with ID: {download_id}")
                print("⏳ Monitoring download progress...")
                
                # Monitor progress with detailed logging
                start_time = time.time()
                last_progress = 0
                
                while True:
                    time.sleep(2)
                    elapsed = time.time() - start_time
                    
                    status_response = requests.get(f"{BASE_URL}/status/{download_id}")
                    
                    if status_response.status_code == 200:
                        status = status_response.json()
                        current_progress = status.get('progress', 0)
                        current_status = status.get('status', 'unknown')
                        message = status.get('message', 'No message')
                        
                        print(f"[{elapsed:.1f}s] Status: {current_status}, Progress: {current_progress}%, Message: {message}")
                        
                        if current_status == 'downloading':
                            if current_progress > last_progress:
                                print(f"  📈 Progress increased: {last_progress}% → {current_progress}%")
                                last_progress = current_progress
                            
                            # Check for stuck progress
                            if elapsed > 60 and current_progress == last_progress:  # 1 minute with no progress
                                print(f"  ⚠️  Progress stuck at {current_progress}% for over 1 minute")
                            
                        elif current_status == 'completed':
                            print(f"  🎉 Download completed in {elapsed:.1f} seconds!")
                            print(f"  📁 Video path: {status.get('video_path', 'N/A')}")
                            print(f"  📝 Title: {status.get('title', 'N/A')}")
                            return True
                            
                        elif current_status == 'error':
                            print(f"  ❌ Download failed: {message}")
                            return False
                            
                        # Timeout after 10 minutes
                        if elapsed > 600:
                            print(f"  ⏰ Test timed out after 10 minutes")
                            return False
                    else:
                        print(f"  ❌ Failed to get status: {status_response.status_code}")
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
    print("🎬 Video GPT Gaming App - Completion Detection Test")
    print("=" * 60)
    
    success = test_download_completion()
    
    if success:
        print("\n🎉 Test completed successfully!")
        print("The download completion detection is working properly.")
    else:
        print("\n💥 Test failed!")
        print("There may be an issue with download completion detection.")
    
    print(f"\n🌐 Open your browser and go to: {BASE_URL}")
    print("Check the browser console for detailed logging")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Test stopped by user")
    except Exception as e:
        print(f"\n💥 Unexpected error: {e}")

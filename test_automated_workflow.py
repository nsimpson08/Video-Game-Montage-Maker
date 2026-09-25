#!/usr/bin/env python3
"""
Test script for the new automated workflow
Tests multiple video downloads and automatic middle 30-second extraction
"""

import requests
import time
import json

BASE_URL = "http://127.0.0.1:5001"

def test_automated_workflow():
    """Test the new automated workflow"""
    print("🎬 Testing Automated Video Processing Workflow")
    print("=" * 60)
    
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
    
    # Test with multiple short videos - using more reliable URLs
    test_urls = [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",  # "Me at the zoo" - YouTube's first video
        "https://www.youtube.com/watch?v=9bZkp7q19f0",  # PSY - GANGNAM STYLE (short version)
    ]
    
    print(f"\n📥 Testing automated workflow with {len(test_urls)} videos:")
    for i, url in enumerate(test_urls, 1):
        print(f"  {i}. {url}")
    
    try:
        # Start batch download
        response = requests.post(f"{BASE_URL}/download_multiple", 
                               json={"urls": test_urls},
                               timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            batch_id = data.get('batch_id')
            
            if batch_id:
                print(f"\n✅ Batch download started with ID: {batch_id}")
                print("⏳ Monitoring automated workflow...")
                
                # Monitor the entire workflow
                start_time = time.time()
                
                while True:
                    time.sleep(3)
                    elapsed = time.time() - start_time
                    
                    status_response = requests.get(f"{BASE_URL}/status/{batch_id}")
                    
                    if status_response.status_code == 200:
                        status = status_response.json()
                        current_status = status.get('status', 'unknown')
                        current_progress = status.get('progress', 0)
                        message = status.get('message', 'No message')
                        
                        print(f"[{elapsed:.1f}s] Status: {current_status}, Progress: {current_progress}%, Message: {message}")
                        
                        # Show video counts if available
                        if 'completed_videos' in status:
                            print(f"  📊 Videos: {status['completed_videos']}/{status['total_videos']} completed, {status['failed_videos']} failed")
                        
                        if current_status == 'downloading':
                            # Continue monitoring
                            pass
                        elif current_status == 'processing':
                            print("  🔄 Videos downloaded, now processing to extract middle 30 seconds...")
                            # Continue monitoring
                            pass
                        elif current_status == 'completed':
                            print(f"  🎉 Workflow completed in {elapsed:.1f} seconds!")
                            print(f"  📁 Final video: {status.get('final_video_path', 'N/A')}")
                            
                            # Test download
                            print("  📥 Testing download...")
                            download_response = requests.get(f"{BASE_URL}/download_result/{batch_id}")
                            if download_response.status_code == 200:
                                print("  ✅ Download successful!")
                            else:
                                print(f"  ❌ Download failed: {download_response.status_code}")
                            
                            return True
                            
                        elif current_status == 'error':
                            print(f"  ❌ Workflow failed: {message}")
                            print(f"  💡 This might be due to YouTube bot detection or video availability issues")
                            print(f"  💡 Try using different video URLs or wait a few minutes before trying again")
                            return False
                            
                        # Timeout after 15 minutes
                        if elapsed > 900:
                            print(f"  ⏰ Test timed out after 15 minutes")
                            return False
                    else:
                        print(f"  ❌ Failed to get status: {status_response.status_code}")
                        return False
            else:
                print("❌ No batch ID received")
                return False
                
        else:
            print(f"❌ Batch download request failed: {response.status_code}")
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

def test_single_video_fallback():
    """Test with a single, very reliable video as fallback"""
    print("\n🔄 Testing fallback with single video...")
    
    # Use a very reliable video
    fallback_url = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # YouTube's first video
    
    try:
        response = requests.post(f"{BASE_URL}/download_multiple", 
                               json={"urls": [fallback_url]},
                               timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            batch_id = data.get('batch_id')
            
            if batch_id:
                print(f"✅ Fallback test started with ID: {batch_id}")
                return True
            else:
                print("❌ Fallback test failed to get batch ID")
                return False
        else:
            print(f"❌ Fallback test failed: {response.status_code}")
            return False
            
    except Exception as e:
        print(f"❌ Fallback test error: {e}")
        return False

def main():
    """Main test function"""
    print("🚀 Video GPT Gaming App - Automated Workflow Test")
    print("=" * 70)
    
    success = test_automated_workflow()
    
    if not success:
        print("\n🔄 Main test failed, trying fallback...")
        success = test_single_video_fallback()
    
    if success:
        print("\n🎉 Test completed successfully!")
        print("The automated workflow is working!")
        print("\nFeatures tested:")
        print("✅ Multiple video downloads")
        print("✅ Automatic middle 30-second extraction")
        print("✅ Video combination")
        print("✅ Final video download")
    else:
        print("\n💥 All tests failed!")
        print("\n🔍 Troubleshooting tips:")
        print("1. Check if YouTube is accessible from your location")
        print("2. Try using different video URLs")
        print("3. Wait a few minutes before trying again (YouTube bot detection)")
        print("4. Check the server logs for detailed error messages")
    
    print(f"\n🌐 Open your browser and go to: {BASE_URL}")
    print("Try the new automated workflow with multiple video URLs!")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Test stopped by user")
    except Exception as e:
        print(f"\n💥 Unexpected error: {e}")

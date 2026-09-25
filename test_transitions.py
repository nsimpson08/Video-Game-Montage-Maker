#!/usr/bin/env python3

import requests
import json
import time

def test_clip_transitions():
    """Test the clip transitions functionality"""
    
    # Test data
    test_data = {
        "urls": [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # Rick Roll (short video)
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"   # Same video for testing
        ],
        "clip_duration": 10,  # Short clips for quick testing
        "audio_option": "original",
        "music_source": "youtube",
        "true_achievements_user_id": "",
        "true_achievements_url": "",
        "add_title_overlays": True,
        "add_fade_effects": True,
        "add_clip_transitions": True,  # This should be True
        "selected_games": []
    }
    
    print("Testing clip transitions...")
    print(f"Request data: {json.dumps(test_data, indent=2)}")
    
    # Send request to the app
    try:
        response = requests.post('http://127.0.0.1:5001/download_multiple', 
                               json=test_data, 
                               headers={'Content-Type': 'application/json'})
        
        if response.status_code == 200:
            result = response.json()
            print(f"✅ Request successful: {result}")
            
            if 'batch_id' in result:
                batch_id = result['batch_id']
                print(f"Batch ID: {batch_id}")
                
                # Monitor the process
                for i in range(30):  # Wait up to 30 seconds
                    time.sleep(2)
                    
                    status_response = requests.get(f'http://127.0.0.1:5001/status/{batch_id}')
                    if status_response.status_code == 200:
                        status = status_response.json()
                        print(f"Status: {status.get('status')} - {status.get('message')}")
                        
                        if status.get('status') == 'completed':
                            print("✅ Process completed successfully!")
                            return True
                        elif status.get('status') == 'error':
                            print(f"❌ Process failed: {status.get('message')}")
                            return False
                    else:
                        print(f"❌ Failed to get status: {status_response.status_code}")
                        return False
                
                print("⏰ Process timed out")
                return False
            else:
                print(f"❌ No batch ID in response: {result}")
                return False
        else:
            print(f"❌ Request failed: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        print(f"❌ Exception: {e}")
        return False

if __name__ == "__main__":
    test_clip_transitions()

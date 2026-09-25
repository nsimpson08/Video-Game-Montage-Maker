#!/usr/bin/env python3
"""
File cleanup system for Railway deployment
Automatically removes old files to control storage costs
"""

import os
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path

class FileCleanup:
    def __init__(self, temp_dir="temp", processed_dir="processed", max_age_hours=24):
        self.temp_dir = Path(temp_dir)
        self.processed_dir = Path(processed_dir)
        self.max_age_hours = max_age_hours
        self.running = False
        
    def cleanup_old_files(self):
        """Remove files older than max_age_hours"""
        current_time = time.time()
        max_age_seconds = self.max_age_hours * 3600
        
        cleaned_files = 0
        freed_space = 0
        
        for directory in [self.temp_dir, self.processed_dir]:
            if not directory.exists():
                continue
                
            for file_path in directory.iterdir():
                if file_path.is_file():
                    file_age = current_time - file_path.stat().st_mtime
                    
                    if file_age > max_age_seconds:
                        try:
                            file_size = file_path.stat().st_size
                            file_path.unlink()
                            cleaned_files += 1
                            freed_space += file_size
                            print(f"Cleaned: {file_path} ({file_size} bytes)")
                        except Exception as e:
                            print(f"Error cleaning {file_path}: {e}")
        
        print(f"Cleanup complete: {cleaned_files} files, {freed_space} bytes freed")
        return cleaned_files, freed_space
    
    def start_cleanup_scheduler(self, interval_hours=1):
        """Start automatic cleanup every interval_hours"""
        self.running = True
        
        def cleanup_loop():
            while self.running:
                try:
                    self.cleanup_old_files()
                    time.sleep(interval_hours * 3600)
                except Exception as e:
                    print(f"Cleanup error: {e}")
                    time.sleep(300)  # Wait 5 minutes on error
        
        cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
        cleanup_thread.start()
        print(f"Cleanup scheduler started (every {interval_hours} hours)")
    
    def stop_cleanup_scheduler(self):
        """Stop the cleanup scheduler"""
        self.running = False

# Global cleanup instance
cleanup_manager = FileCleanup()

def start_cleanup():
    """Start the cleanup system"""
    cleanup_manager.start_cleanup_scheduler(interval_hours=1)

def cleanup_now():
    """Run cleanup immediately"""
    return cleanup_manager.cleanup_old_files()

if __name__ == "__main__":
    # Test cleanup
    cleanup_now()

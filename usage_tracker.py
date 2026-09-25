#!/usr/bin/env python3
"""
Usage tracking and rate limiting for Railway deployment
Helps control costs by limiting resource usage
"""

import time
import json
from datetime import datetime, timedelta
from pathlib import Path

class UsageTracker:
    def __init__(self, max_daily_users=100, max_file_size_mb=100):
        self.max_daily_users = max_daily_users
        self.max_file_size_mb = max_file_size_mb
        self.usage_file = Path("usage_data.json")
        self.usage_data = self.load_usage_data()
    
    def load_usage_data(self):
        """Load usage data from file"""
        if self.usage_file.exists():
            try:
                with open(self.usage_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {
            "daily_users": {},
            "total_processed": 0,
            "total_storage_mb": 0
        }
    
    def save_usage_data(self):
        """Save usage data to file"""
        with open(self.usage_file, 'w') as f:
            json.dump(self.usage_data, f)
    
    def can_process_video(self, user_ip, file_size_mb):
        """Check if user can process a video"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Check daily user limit
        if today not in self.usage_data["daily_users"]:
            self.usage_data["daily_users"][today] = set()
        
        if len(self.usage_data["daily_users"][today]) >= self.max_daily_users:
            return False, "Daily user limit reached"
        
        # Check file size limit
        if file_size_mb > self.max_file_size_mb:
            return False, f"File too large (max {self.max_file_size_mb}MB)"
        
        # Check storage limit
        if self.usage_data["total_storage_mb"] > 1000:  # 1GB limit
            return False, "Storage limit reached"
        
        return True, "OK"
    
    def record_usage(self, user_ip, file_size_mb, processing_time):
        """Record video processing usage"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Add user to daily count
        if today not in self.usage_data["daily_users"]:
            self.usage_data["daily_users"][today] = set()
        
        self.usage_data["daily_users"][today].add(user_ip)
        self.usage_data["total_processed"] += 1
        self.usage_data["total_storage_mb"] += file_size_mb
        
        self.save_usage_data()
    
    def get_usage_stats(self):
        """Get current usage statistics"""
        today = datetime.now().strftime("%Y-%m-%d")
        daily_users = len(self.usage_data["daily_users"].get(today, set()))
        
        return {
            "daily_users": daily_users,
            "max_daily_users": self.max_daily_users,
            "total_processed": self.usage_data["total_processed"],
            "total_storage_mb": self.usage_data["total_storage_mb"],
            "storage_limit_mb": 1000
        }
    
    def cleanup_old_data(self, days_to_keep=7):
        """Remove old usage data"""
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        
        old_dates = []
        for date_str in self.usage_data["daily_users"]:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            if date_obj < cutoff_date:
                old_dates.append(date_str)
        
        for date_str in old_dates:
            del self.usage_data["daily_users"][date_str]
        
        self.save_usage_data()
        print(f"Cleaned up {len(old_dates)} old usage records")

# Global usage tracker
usage_tracker = UsageTracker()

def check_usage_limit(user_ip, file_size_mb):
    """Check if user can process video"""
    return usage_tracker.can_process_video(user_ip, file_size_mb)

def record_usage(user_ip, file_size_mb, processing_time):
    """Record video processing usage"""
    usage_tracker.record_usage(user_ip, file_size_mb, processing_time)

def get_usage_stats():
    """Get usage statistics"""
    return usage_tracker.get_usage_stats()

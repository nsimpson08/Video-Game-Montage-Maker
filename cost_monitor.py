#!/usr/bin/env python3
"""
Cost monitoring and automatic shutdown system for Railway
Prevents runaway costs by monitoring usage and stopping the app
"""

import os
import time
import json
import psutil
from datetime import datetime, timedelta
from pathlib import Path

class CostMonitor:
    def __init__(self, 
                 max_daily_cost=10,  # $10 per day
                 max_storage_gb=5,   # 5GB storage
                 max_cpu_percent=80, # 80% CPU usage
                 max_memory_gb=2):   # 2GB memory
        self.max_daily_cost = max_daily_cost
        self.max_storage_gb = max_storage_gb
        self.max_cpu_percent = max_cpu_percent
        self.max_memory_gb = max_memory_gb
        self.monitor_file = Path("cost_monitor.json")
        self.monitor_data = self.load_monitor_data()
        
        # Cost estimation (rough estimates)
        self.cost_per_gb_storage = 0.10  # $0.10 per GB per day
        self.cost_per_cpu_hour = 0.05    # $0.05 per CPU hour
        self.cost_per_memory_gb_hour = 0.02  # $0.02 per GB memory per hour
    
    def load_monitor_data(self):
        """Load monitoring data from file"""
        if self.monitor_file.exists():
            try:
                with open(self.monitor_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {
            "daily_costs": {},
            "total_cost": 0,
            "last_reset": datetime.now().strftime("%Y-%m-%d"),
            "shutdown_triggered": False
        }
    
    def save_monitor_data(self):
        """Save monitoring data to file"""
        with open(self.monitor_file, 'w') as f:
            json.dump(self.monitor_data, f)
    
    def get_current_usage(self):
        """Get current system usage"""
        # CPU usage
        cpu_percent = psutil.cpu_percent(interval=1)
        
        # Memory usage
        memory = psutil.virtual_memory()
        memory_gb = memory.used / (1024**3)
        
        # Storage usage
        storage_gb = 0
        for path in ["uploads", "processed", "temp"]:
            if Path(path).exists():
                storage_gb += sum(f.stat().st_size for f in Path(path).rglob('*') if f.is_file()) / (1024**3)
        
        return {
            "cpu_percent": cpu_percent,
            "memory_gb": memory_gb,
            "storage_gb": storage_gb,
            "timestamp": datetime.now().isoformat()
        }
    
    def calculate_daily_cost(self):
        """Calculate estimated daily cost"""
        usage = self.get_current_usage()
        today = datetime.now().strftime("%Y-%m-%d")
        
        # Storage cost
        storage_cost = usage["storage_gb"] * self.cost_per_gb_storage
        
        # CPU cost (assuming 24 hours)
        cpu_cost = (usage["cpu_percent"] / 100) * self.cost_per_cpu_hour * 24
        
        # Memory cost (assuming 24 hours)
        memory_cost = usage["memory_gb"] * self.cost_per_memory_gb_hour * 24
        
        total_cost = storage_cost + cpu_cost + memory_cost
        
        # Update daily costs
        if today not in self.monitor_data["daily_costs"]:
            self.monitor_data["daily_costs"][today] = 0
        
        self.monitor_data["daily_costs"][today] = max(
            self.monitor_data["daily_costs"][today], 
            total_cost
        )
        
        return total_cost
    
    def check_limits(self):
        """Check if any limits are exceeded"""
        usage = self.get_current_usage()
        daily_cost = self.calculate_daily_cost()
        
        violations = []
        
        # Check daily cost limit
        if daily_cost > self.max_daily_cost:
            violations.append(f"Daily cost limit exceeded: ${daily_cost:.2f} > ${self.max_daily_cost}")
        
        # Check storage limit
        if usage["storage_gb"] > self.max_storage_gb:
            violations.append(f"Storage limit exceeded: {usage['storage_gb']:.2f}GB > {self.max_storage_gb}GB")
        
        # Check CPU limit
        if usage["cpu_percent"] > self.max_cpu_percent:
            violations.append(f"CPU limit exceeded: {usage['cpu_percent']:.1f}% > {self.max_cpu_percent}%")
        
        # Check memory limit
        if usage["memory_gb"] > self.max_memory_gb:
            violations.append(f"Memory limit exceeded: {usage['memory_gb']:.2f}GB > {self.max_memory_gb}GB")
        
        return violations
    
    def should_shutdown(self):
        """Determine if the app should shutdown"""
        if self.monitor_data["shutdown_triggered"]:
            return True
        
        violations = self.check_limits()
        if violations:
            print("COST LIMIT VIOLATIONS DETECTED:")
            for violation in violations:
                print(f"  - {violation}")
            
            # Trigger shutdown
            self.monitor_data["shutdown_triggered"] = True
            self.save_monitor_data()
            return True
        
        return False
    
    def get_status(self):
        """Get current monitoring status"""
        usage = self.get_current_usage()
        daily_cost = self.calculate_daily_cost()
        violations = self.check_limits()
        
        return {
            "usage": usage,
            "daily_cost": daily_cost,
            "max_daily_cost": self.max_daily_cost,
            "violations": violations,
            "shutdown_triggered": self.monitor_data["shutdown_triggered"],
            "limits": {
                "max_daily_cost": self.max_daily_cost,
                "max_storage_gb": self.max_storage_gb,
                "max_cpu_percent": self.max_cpu_percent,
                "max_memory_gb": self.max_memory_gb
            }
        }
    
    def reset_daily_costs(self):
        """Reset daily costs (call this daily)"""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.monitor_data["last_reset"] != today:
            self.monitor_data["daily_costs"] = {today: 0}
            self.monitor_data["last_reset"] = today
            self.monitor_data["shutdown_triggered"] = False
            self.save_monitor_data()

# Global cost monitor
cost_monitor = CostMonitor()

def check_cost_limits():
    """Check if cost limits are exceeded"""
    return cost_monitor.should_shutdown()

def get_cost_status():
    """Get current cost monitoring status"""
    return cost_monitor.get_status()

def reset_daily_costs():
    """Reset daily costs"""
    cost_monitor.reset_daily_costs()

if __name__ == "__main__":
    # Test cost monitoring
    status = get_cost_status()
    print("Cost Monitor Status:")
    print(f"  Daily Cost: ${status['daily_cost']:.2f} / ${status['max_daily_cost']}")
    print(f"  Storage: {status['usage']['storage_gb']:.2f}GB / {status['limits']['max_storage_gb']}GB")
    print(f"  CPU: {status['usage']['cpu_percent']:.1f}% / {status['limits']['max_cpu_percent']}%")
    print(f"  Memory: {status['usage']['memory_gb']:.2f}GB / {status['limits']['max_memory_gb']}GB")
    
    if status['violations']:
        print("  VIOLATIONS:")
        for violation in status['violations']:
            print(f"    - {violation}")
    else:
        print("  All limits OK")

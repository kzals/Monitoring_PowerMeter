#!/usr/bin/env python3
"""Verify that data has been written to InfluxDB"""

import os
from pathlib import Path
from datetime import datetime, timedelta, timezone
from influxdb_client import InfluxDBClient

# Load .env.example
env_file = Path(".env.example")
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                if key and key not in os.environ:
                    os.environ[key] = value

# Get settings
url = os.getenv("INFLUX_URL", "http://127.0.0.1:8086")
token = os.getenv("INFLUX_TOKEN", "")
org = os.getenv("INFLUX_ORG", "")
bucket = os.getenv("INFLUX_BUCKET", "powermeter")

print(f"Connecting to InfluxDB: {url}")
print(f"Org: {org}, Bucket: {bucket}\n")

try:
    client = InfluxDBClient(url=url, token=token, org=org)
    query_api = client.query_api()
    
    # Query last 10 minutes of data
    now = datetime.now(timezone.utc)
    start_time = (now - timedelta(minutes=10)).isoformat()
    end_time = now.isoformat()
    
    query = f'''
    from(bucket: "{bucket}")
    |> range(start: {start_time}, stop: {end_time})
    |> filter(fn: (r) => r._measurement == "pm2220")
    |> sort(columns: ["_time"], desc: true)
    |> limit(n: 100)
    '''
    
    result = query_api.query(org=org, query=query)
    
    if not result:
        print("❌ No data found in InfluxDB")
    else:
        print("✅ Data found in InfluxDB:\n")
        count = 0
        for table in result:
            for record in table.records:
                count += 1
                print(f"  Time: {record.get_time()}")
                print(f"  Device: {record.values.get('device', 'N/A')}")
                print(f"  Field: {record.get_field()}")
                print(f"  Value: {record.get_value()}\n")
        print(f"Total records: {count}")
    
    client.close()
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()

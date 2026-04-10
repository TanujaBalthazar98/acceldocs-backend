#!/usr/bin/env python
"""
Quick script to help find your AccelDocs credentials.
"""
import os
import sys

# Check env file
env_path = os.path.expanduser("~/.env")
if os.path.exists(env_path):
    print("Checking ~/.env for credentials...")
    with open(env_path) as f:
        for line in f:
            if any(x in line.upper() for x in ["TOKEN", "ORG", "PRODUCT", "ACCEL"]):
                if "=" in line:
                    key = line.split("=")[0].strip()
                    print(f"  Found: {key}")

# Check current directory
print("\nChecking current .env...")
env_files = [".env", ".env.local", ".env.production"]
for ef in env_files:
    if os.path.exists(ef):
        with open(ef) as f:
            for line in f:
                if "ACCELDOCS" in line and "=" in line:
                    print(f"  {line.strip()}")

print("\n" + "="*50)
print("To find credentials manually:")
print("="*50)
print("""
1. Log into AccelDocs at: https://acceldocs-backend.vercel.app
2. Check browser URL for org name 
3. Go to Settings/API page
4. Generate/create API token
5. Note your Org ID and Product ID

Or ask whoever set up AccelDocs for the credentials.
""")
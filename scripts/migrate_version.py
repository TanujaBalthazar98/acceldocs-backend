#!/usr/bin/env python
"""
Simple script to migrate ONE product at a time.

Usage:
    # Latest version only (fast):
    python scripts/migrate_version.py --source https://docs.acceldata.io --product pulse --dry-run
    python scripts/migrate_version.py --source https://docs.acceldata.io --product odp --dry-run
    python scripts/migrate_version.py --source https://docs.acceldata.io --product adoc --dry-run

    # Specific version (needs --all-versions):
    python scripts/migrate_version.py --source https://docs.acceldata.io --product pulse --all-versions --version-idx 0 --dry-run
    python scripts/migrate_version.py --source https://docs.acceldata.io --product pulse --all-versions --version-idx 1 --dry-run
"""
import sys

if __name__ == "__main__":
    # Just call the main script
    from scripts.migrate_developerhub import main as migrate_main
    sys.argv = [sys.argv[0]] + sys.argv[1:]
    migrate_main()
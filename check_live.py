"""Check that every real data source works from this computer.

Run it from the project folder, with the virtual environment active:

    python check_live.py

It tries each archive the site depends on and prints OK or FAILED with the reason. The same
check runs on the live website at /api/diagnose.
"""

from __future__ import annotations

import sys

from spherex_viewer import diagnose

report = diagnose.run()
for item in report["archives"]:
    print(f"[{'OK' if item['ok'] else 'FAILED':6}] {item['name']} ({item['seconds']} s)")
    if not item["ok"]:
        print(f"         {item['detail']}")
print("=" * 60)
print(
    f"{len(report['archives']) - len(report['failed'])} of {len(report['archives'])} checks passed."
)
if report["failed"]:
    print("If 'SPHEREx image search' failed, objects saved with save_images.py still work.")
sys.exit(1 if report["failed"] else 0)

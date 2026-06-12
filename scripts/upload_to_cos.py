#!/usr/bin/env python3
"""Tencent COS upload wrapper for video-studio.

Replaces upload_to_oss.py (R2) for video outputs. Reads credentials from
skills/agent-memory/memories/storage/zyzyz-cos-storage.md or env vars.
Uses native COS endpoint (openclaw-1325869979.cos.na-siliconvalley.myqcloud.com).

Usage:
  upload_to_cos.py --file <local> --key <key>       # uploads and prints presigned URL
  upload_to_cos.py --file <local> --key <key> --ttl 86400
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="Local file to upload")
    ap.add_argument("--key", required=True, help="Object key in bucket")
    ap.add_argument("--ttl", type=int, default=7 * 24 * 3600, help="Presigned URL TTL seconds (max 7d)")
    ap.add_argument("--content-type", default="video/mp4", help="MIME type")
    args = ap.parse_args()

    cos_helper = Path("/root/.openclaw/workspace/scripts/cos_helper.py")
    if not cos_helper.exists():
        raise SystemExit(f"✗ cos_helper.py not found at {cos_helper}")

    src = Path(args.file)
    if not src.exists():
        raise SystemExit(f"✗ File not found: {args.file}")
    print(f"↑ {args.file} ({src.stat().st_size:,} bytes)", file=sys.stderr)

    # Upload
    upload = subprocess.run(
        ["python3", str(cos_helper), "upload", str(src), args.key,
         "--content-type", args.content_type],
        capture_output=True, text=True, timeout=300,
    )
    if upload.returncode != 0:
        print(f"✗ upload failed: {upload.stderr or upload.stdout}", file=sys.stderr)
        raise SystemExit(1)

    # Generate presigned URL via --out (writes URL to side file)
    out_file = Path("/tmp/cos_presigned_url.txt")
    if out_file.exists():
        out_file.unlink()
    sign = subprocess.run(
        ["python3", str(cos_helper), "sign", args.key,
         "--ttl", str(args.ttl), "--out", str(out_file)],
        capture_output=True, text=True, timeout=30,
    )
    if sign.returncode != 0 or not out_file.exists():
        print(f"✗ sign failed: {sign.stderr or sign.stdout}", file=sys.stderr)
        raise SystemExit(1)

    url = out_file.read_text().strip()
    # Cleanup the side file
    out_file.unlink()
    print(url, file=sys.stdout)


if __name__ == "__main__":
    main()

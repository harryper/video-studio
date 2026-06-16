#!/usr/bin/env python3
"""Upload a video-studio artifact to Cloudflare R2."""

import argparse
import re
from pathlib import Path

import boto3


BUCKET = "openclaw"
CREDENTIALS_FILE = (
    Path.home()
    / ".openclaw/workspace/skills/agent-memory/memories/storage/r2-oss-media-upload.md"
)
MAX_TTL = 7 * 24 * 3600


def load_r2_config():
    text = CREDENTIALS_FILE.read_text(encoding="utf-8")

    def field(name):
        match = re.search(rf"^- {re.escape(name)}:\s*`([^`]+)`", text, re.MULTILINE)
        if not match:
            raise RuntimeError(f"Missing {name} in {CREDENTIALS_FILE}")
        return match.group(1)

    return field("endpoint"), field("access_key_id"), field("secret_access_key")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--ttl", type=int, default=MAX_TTL)
    parser.add_argument("--content-type", default="video/mp4")
    args = parser.parse_args()

    source = Path(args.file)
    if not source.is_file():
        raise SystemExit(f"Source file not found: {source}")

    endpoint, access_key, secret_key = load_r2_config()
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )
    client.upload_file(
        str(source),
        BUCKET,
        args.key,
        ExtraArgs={"ContentType": args.content_type},
    )
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": args.key},
        ExpiresIn=min(max(args.ttl, 1), MAX_TTL),
    )
    print(url)


if __name__ == "__main__":
    main()

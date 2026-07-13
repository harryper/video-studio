#!/usr/bin/env python3
"""Upload a video-studio artifact to Cloudflare R2.

Credentials resolution order:
  1. $R2_CREDENTIALS_FILE — explicit path, useful for ops
  2. <repo>/scripts/r2_credentials.md — project-internal, gitignored.
     Operationally maintained by `cp` from the original storage location;
     this script never reads the original directly so the repo stays
     decoupled from any specific skills/ path.
  3. ~/.openclaw/.../r2-oss-media-upload.md — legacy fallback. Logs a
     deprecation warning; will be removed once ops migrate fully.
"""

import argparse
import os
import re
import sys
from pathlib import Path

import boto3


BUCKET = "openclaw"
SCRIPT_DIR = Path(__file__).resolve().parent
_DEFAULT_PROJECT_CREDS = SCRIPT_DIR / "r2_credentials.md"
_LEGACY_FALLBACK = (
    Path.home()
    / ".openclaw/workspace/skills/agent-memory/memories/storage/r2-oss-media-upload.md"
)
MAX_TTL = 7 * 24 * 3600


def _resolve_credentials_file() -> Path:
    env = os.environ.get("R2_CREDENTIALS_FILE")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise SystemExit(f"R2_CREDENTIALS_FILE points at missing file: {p}")
        return p
    if _DEFAULT_PROJECT_CREDS.is_file():
        return _DEFAULT_PROJECT_CREDS
    if _LEGACY_FALLBACK.is_file():
        print(
            f"[upload_to_oss] WARNING: falling back to legacy credentials at "
            f"{_LEGACY_FALLBACK}; copy it into {_DEFAULT_PROJECT_CREDS} "
            "(gitignored) and set R2_CREDENTIALS_FILE to silence this.",
            file=sys.stderr,
        )
        return _LEGACY_FALLBACK
    raise SystemExit(
        "R2 credentials not found. Set R2_CREDENTIALS_FILE, place file at "
        f"{_DEFAULT_PROJECT_CREDS}, or restore {_LEGACY_FALLBACK}."
    )


def load_r2_config():
    path = _resolve_credentials_file()
    text = path.read_text(encoding="utf-8")

    def field(name):
        match = re.search(rf"^- {re.escape(name)}:\s*`([^`]+)`", text, re.MULTILINE)
        if not match:
            raise RuntimeError(f"Missing {name} in {path}")
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

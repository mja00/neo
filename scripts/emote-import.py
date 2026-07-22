#!/usr/bin/env python3
"""Import a folder of images (e.g. exported Discord emotes) into Matrix as an
MSC2545 image pack, so they render inline as :shortcode: in Element and other
im.ponies-aware clients.

Each file becomes an emote whose shortcode is its filename (without extension).
By default the pack is installed as personal account data (im.ponies.user_emotes),
available to you in every room; pass --room to install it as room state instead
(needs permission to send state in that room).

Only the Python standard library is used, so it runs anywhere python3 exists:

    MATRIX_ACCESS_TOKEN=syt_... ./scripts/emote-import.py --pack-name discord ./my-emotes

Get the token from Element -> Settings -> Help & About -> Advanced -> Access Token.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Extensions Matrix clients render as inline emoji, mapped to their MIME type.
CONTENT_TYPES = {
    ".png": "image/png",
    ".apng": "image/apng",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

USER_EMOTES = "im.ponies.user_emotes"
ROOM_EMOTES = "im.ponies.room_emotes"


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def homeserver_from_env_file() -> str | None:
    # Fall back to the deployment's own .env so the token is the only thing to pass.
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.is_file():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("NEO_MATRIX_HOST="):
            host = line.split("=", 1)[1].strip()
            return f"https://{host}" if host else None
    return None


def request(method: str, url: str, token: str, *, body: bytes | None = None,
            content_type: str | None = None) -> dict:
    # A real UA avoids Cloudflare bot-fight (error 1010) blocking Python-urllib.
    headers = {"Authorization": f"Bearer {token}",
               "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) neo-emote-import/1.0"}
    if content_type:
        headers["Content-Type"] = content_type
    # Retry on 429 so a bulk import isn't killed by Synapse's media rate limiter.
    for attempt in range(6):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 429 and attempt < 5:
                try:
                    wait = json.loads(detail).get("retry_after_ms", 1000) / 1000
                except ValueError:
                    wait = 1.0
                time.sleep(min(wait + 0.1, 10))
                continue
            raise RuntimeError(f"{method} {url} -> {e.code}: {detail}") from e


def shortcode(filename: str) -> str:
    # Keep it to what clients accept in :name: autocomplete; drop anything else.
    stem = Path(filename).stem
    return re.sub(r"[^A-Za-z0-9_-]", "_", stem)


def main() -> None:
    ap = argparse.ArgumentParser(description="Import images as an MSC2545 emote pack.")
    ap.add_argument("folder", help="Directory of image files to import.")
    ap.add_argument("--pack-name", help="Pack display name (default: folder name).")
    ap.add_argument("--homeserver", help="Base URL, e.g. https://matrix.example.com "
                                         "(default: NEO_MATRIX_HOST from .env).")
    ap.add_argument("--token", help="Access token (default: $MATRIX_ACCESS_TOKEN).")
    ap.add_argument("--room", help="Install as room state in this room ID instead of "
                                   "personal account data.")
    args = ap.parse_args()

    token = args.token or os.environ.get("MATRIX_ACCESS_TOKEN")
    if not token:
        die("no access token — pass --token or set MATRIX_ACCESS_TOKEN.")

    hs = (args.homeserver or homeserver_from_env_file() or "").rstrip("/")
    if not hs:
        die("no homeserver — pass --homeserver or run from a neo checkout with NEO_MATRIX_HOST set.")

    folder = Path(args.folder)
    if not folder.is_dir():
        die(f"{folder} is not a directory.")
    pack_name = args.pack_name or folder.name

    images = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in CONTENT_TYPES)
    if not images:
        die(f"no importable images in {folder} (want: {', '.join(sorted(CONTENT_TYPES))}).")

    user_id = request("GET", f"{hs}/_matrix/client/v3/account/whoami", token)["user_id"]
    print(f"Importing {len(images)} emote(s) into pack '{pack_name}' as {user_id} on {hs}")

    new_images: dict[str, dict] = {}
    for path in images:
        code = shortcode(path.name)
        data = path.read_bytes()
        ctype = CONTENT_TYPES[path.suffix.lower()]
        upload = request(
            "POST",
            f"{hs}/_matrix/media/v3/upload?filename={urllib.parse.quote(path.name)}",
            token, body=data, content_type=ctype,
        )
        mxc = upload["content_uri"]
        new_images[code] = {
            "url": mxc,
            "usage": ["emoticon"],
            "info": {"mimetype": ctype, "size": len(data)},
        }
        print(f"  :{code}: -> {mxc}")

    if args.room:
        # Room-level pack: one state event, keyed by the pack name (slug).
        state_key = re.sub(r"[^a-z0-9_-]", "-", pack_name.lower())
        url = (f"{hs}/_matrix/client/v3/rooms/{urllib.parse.quote(args.room)}"
               f"/state/{ROOM_EMOTES}/{urllib.parse.quote(state_key)}")
        pack = {"pack": {"display_name": pack_name, "usage": ["emoticon"]},
                "images": new_images}
        request("PUT", url, token, body=json.dumps(pack).encode(),
                content_type="application/json")
        print(f"Installed as room emote pack '{state_key}' in {args.room}.")
        return

    # Personal pack: merge into existing account data so repeat runs accumulate.
    url = (f"{hs}/_matrix/client/v3/user/{urllib.parse.quote(user_id)}"
           f"/account_data/{USER_EMOTES}")
    try:
        existing = request("GET", url, token)
    except RuntimeError as e:
        if " 404:" not in str(e):
            raise
        existing = {}  # no pack yet — start fresh
    merged = existing.get("images", {})
    merged.update(new_images)
    pack = {"pack": existing.get("pack") or {"display_name": pack_name, "usage": ["emoticon"]},
            "images": merged}
    request("PUT", url, token, body=json.dumps(pack).encode(),
            content_type="application/json")
    print(f"Installed {len(new_images)} emote(s) into your personal pack "
          f"({len(merged)} total). Type :{shortcode(images[0].name)}: in Element to use it.")


if __name__ == "__main__":
    main()

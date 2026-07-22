#!/usr/bin/env python3
"""Import a folder of images (e.g. exported Discord/neofox emotes) into Matrix.

Uploads each image once, then installs it as either or both of:

  * inline emotes  — an MSC2545 image pack (im.ponies) so MSC2545-aware clients
                     (SchildiChat, Cinny, FluffyChat, Nheko) autocomplete :name:.
                     NOTE: Element Web/Desktop does NOT render these inline.
  * stickers       — a maunium sticker-picker pack (--stickers), written into the
                     picker's web/packs so it shows in Element's sticker button.

Each file's name (without extension) becomes its shortcode. Only the Python
standard library is used, so it runs anywhere python3 exists:

    # inline emotes (default), personal pack:
    MATRIX_ACCESS_TOKEN=mct_... ./scripts/emote-import.py --pack-name neofox ./neofox
    # also add them to the sticker picker (run where data/stickerpicker lives):
    MATRIX_ACCESS_TOKEN=mct_... ./scripts/emote-import.py --pack-name neofox --stickers ./neofox

Get a long-lived token with `./scripts/mas-admin-token.sh <user>` (MAS deployments);
Element's UI access token is short-lived and will 401 mid-import.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Extensions Matrix clients render as inline emoji/stickers, mapped to MIME type.
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


def default_packs_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data/stickerpicker/web/packs"


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


def image_dims(data: bytes, ext: str) -> tuple[int, int] | None:
    # Best-effort width/height for the sticker info block; None when unknown.
    try:
        if ext in (".png", ".apng") and data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        if ext == ".gif" and data[:3] == b"GIF":
            w, h = struct.unpack("<HH", data[6:10])
            return w, h
    except struct.error:
        return None
    return None


def write_sticker_pack(packs_dir: Path, pack_id: str, title: str,
                       uploaded: list[dict]) -> None:
    # maunium picker format: web/packs/<id>.json + an index.json listing pack files.
    stickers = []
    for u in uploaded:
        info = {"mimetype": u["ctype"], "size": u["size"],
                "thumbnail_url": u["mxc"],
                "thumbnail_info": {"mimetype": u["ctype"], "size": u["size"]}}
        if u["dims"]:
            info["w"], info["h"] = u["dims"]
            info["thumbnail_info"]["w"], info["thumbnail_info"]["h"] = u["dims"]
        stickers.append({"body": u["code"], "info": info, "msgtype": "m.sticker",
                         "url": u["mxc"], "id": u["mxc"].rsplit("/", 1)[-1]})

    packs_dir.mkdir(parents=True, exist_ok=True)
    pack_file = f"{pack_id}.json"
    (packs_dir / pack_file).write_text(
        json.dumps({"title": title, "id": pack_id, "stickers": stickers}))

    index_path = packs_dir / "index.json"
    try:
        index = json.loads(index_path.read_text())
        if not isinstance(index, dict):
            index = {}
    except (FileNotFoundError, ValueError):
        index = {}
    packs = index.get("packs", [])
    if pack_file not in packs:
        packs.append(pack_file)
    index["packs"] = packs
    index_path.write_text(json.dumps(index))
    print(f"Wrote sticker pack {packs_dir/pack_file} ({len(stickers)} stickers) "
          f"and updated index.json.")


def install_emotes(hs: str, token: str, user_id: str, pack_name: str,
                   room: str | None, new_images: dict[str, dict]) -> None:
    if room:
        # Room-level pack: one state event, keyed by the pack name (slug).
        state_key = re.sub(r"[^a-z0-9_-]", "-", pack_name.lower())
        url = (f"{hs}/_matrix/client/v3/rooms/{urllib.parse.quote(room)}"
               f"/state/{ROOM_EMOTES}/{urllib.parse.quote(state_key)}")
        pack = {"pack": {"display_name": pack_name, "usage": ["emoticon"]},
                "images": new_images}
        request("PUT", url, token, body=json.dumps(pack).encode(),
                content_type="application/json")
        print(f"Installed {len(new_images)} inline emote(s) as room pack "
              f"'{state_key}' in {room}.")
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
    print(f"Installed {len(new_images)} inline emote(s) into your personal pack "
          f"({len(merged)} total). Note: Element does not show these inline; "
          f"use --stickers for Element, or an MSC2545 client.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Import images as Matrix emotes and/or stickers.")
    ap.add_argument("folder", help="Directory of image files to import.")
    ap.add_argument("--pack-name", help="Pack display name (default: folder name).")
    ap.add_argument("--homeserver", help="Base URL, e.g. https://matrix.example.com "
                                         "(default: NEO_MATRIX_HOST from .env).")
    ap.add_argument("--token", help="Access token (default: $MATRIX_ACCESS_TOKEN).")
    ap.add_argument("--room", help="Install inline emotes as room state in this room "
                                   "ID instead of personal account data.")
    ap.add_argument("--stickers", action="store_true",
                    help="Also write a maunium sticker-picker pack (for Element).")
    ap.add_argument("--no-emotes", action="store_true",
                    help="Skip the inline im.ponies pack (e.g. stickers only).")
    ap.add_argument("--packs-dir", type=Path, default=default_packs_dir(),
                    help="Where --stickers writes packs (default: the picker's web/packs).")
    args = ap.parse_args()

    if args.no_emotes and not args.stickers:
        die("--no-emotes with no --stickers would do nothing.")

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
    pack_id = re.sub(r"[^a-z0-9_-]", "-", pack_name.lower())

    images = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in CONTENT_TYPES)
    if not images:
        die(f"no importable images in {folder} (want: {', '.join(sorted(CONTENT_TYPES))}).")

    user_id = request("GET", f"{hs}/_matrix/client/v3/account/whoami", token)["user_id"]
    print(f"Uploading {len(images)} image(s) for pack '{pack_name}' as {user_id} on {hs}")

    new_images: dict[str, dict] = {}
    uploaded: list[dict] = []
    for path in images:
        code = shortcode(path.name)
        data = path.read_bytes()
        ctype = CONTENT_TYPES[path.suffix.lower()]
        mxc = request(
            "POST",
            f"{hs}/_matrix/media/v3/upload?filename={urllib.parse.quote(path.name)}",
            token, body=data, content_type=ctype,
        )["content_uri"]
        new_images[code] = {"url": mxc, "usage": ["emoticon"],
                            "info": {"mimetype": ctype, "size": len(data)}}
        uploaded.append({"code": code, "mxc": mxc, "size": len(data),
                         "ctype": ctype, "dims": image_dims(data, path.suffix.lower())})
        print(f"  :{code}: -> {mxc}")

    if not args.no_emotes:
        install_emotes(hs, token, user_id, pack_name, args.room, new_images)
    if args.stickers:
        write_sticker_pack(args.packs_dir, pack_id, pack_name, uploaded)


if __name__ == "__main__":
    main()

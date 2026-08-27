#!/usr/bin/env python3
"""Active-device push gatekeeper for Matrix.

Holds phone pushes while the user is active on a desktop client, cancels
them when the room is read or replied to, and escalates to the real push
gateway after a timeout — Discord-style push semantics for any Matrix
client, with no phone-side reconfiguration.

It works by intercepting at the pusher level: each managed mobile pusher's
`data.url` is rewritten to point at this service, which then implements the
push-gateway notify endpoint and forwards payloads verbatim to the original
gateway (matrix.org sygnal, ntfy, ...) once the hold ends.
"""

import asyncio
import json
import os
import signal
import sys
import time
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, web

STATE_PATH = "/data/state.json"
SYNC_TIMEOUT_MS = 30000
# The sync long-poll returns after ~30s; give the request more read headroom.
SYNC_CLIENT_TIMEOUT = ClientTimeout(total=None, sock_read=70)

# Fields accepted by POST /pushers/set. The MSC3881 fields are response-only
# and must not be re-POSTed (they are unknown to the write path).
PUSHER_BODY_FIELDS = (
    "kind",
    "app_id",
    "app_display_name",
    "device_display_name",
    "pushkey",
    "lang",
    "profile_tag",
    "data",
)


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


class AuthError(Exception):
    """Synapse rejected the access token (401)."""


def parse_users(raw: str) -> dict:
    """Parse 'mxid=token' pairs; split on the FIRST '=' so tokens may contain '='."""
    users = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        mxid, _, token = part.partition("=")
        mxid, token = mxid.strip(), token.strip()
        if not mxid or not token:
            log(f"warning: ignoring malformed NOTIFIER_USERS entry '{part}'")
            continue
        users[mxid] = token
    return users


def clean_pusher(pusher: dict) -> dict:
    """Drop response-only fields; the result is a valid pusher/set body (minus append)."""
    return {
        k: pusher[k]
        for k in PUSHER_BODY_FIELDS
        if k in pusher and pusher[k] is not None
    }


def rewrite_body(pusher: dict, gateway_url: str) -> dict:
    """Copy of the pusher body with data.url pointed at this service."""
    body = dict(pusher)
    body["data"] = dict(pusher.get("data") or {})
    body["data"]["url"] = gateway_url
    return body


class Config:
    def __init__(
        self,
        users=None,
        homeserver_url="http://synapse:8008",
        gateway_url="http://notifier:8080/_matrix/push/v1/notify",
        port=8080,
        escalate_after=300,
        hold_window=120,
        ack_grace=60,
        tick=15,
        mobile_prefixes=("io.element.elementx", "im.vector.app"),
        mobile_device_ids=(),
        state_path=STATE_PATH,
    ):
        self.users = users or {}
        self.homeserver_url = homeserver_url
        self.gateway_url = gateway_url
        self.port = port
        self.escalate_after = escalate_after
        self.hold_window = hold_window
        self.ack_grace = ack_grace
        self.tick = tick
        self.mobile_prefixes = tuple(mobile_prefixes)
        self.mobile_device_ids = set(mobile_device_ids)
        self.state_path = state_path

    @classmethod
    def from_env(cls, environ=None):
        env = environ if environ is not None else os.environ

        def int_env(name, default):
            raw = env.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                log(f"warning: {name}='{raw}' is not an integer — using {default}")
                return default

        prefixes = tuple(
            p.strip()
            for p in env.get(
                "NOTIFIER_MOBILE_APP_ID_PREFIXES", "io.element.elementx,im.vector.app"
            ).split(",")
            if p.strip()
        )
        device_ids = {
            d.strip()
            for d in env.get("NOTIFIER_MOBILE_DEVICE_IDS", "").split(",")
            if d.strip()
        }
        return cls(
            users=parse_users(env.get("NOTIFIER_USERS", "")),
            homeserver_url=env.get("NOTIFIER_HOMESERVER_URL", "http://synapse:8008"),
            gateway_url=env.get(
                "NOTIFIER_GATEWAY_URL", "http://notifier:8080/_matrix/push/v1/notify"
            ),
            port=int_env("NOTIFIER_PORT", 8080),
            escalate_after=int_env("NOTIFIER_ESCALATE_AFTER", 300),
            hold_window=int_env("NOTIFIER_HOLD_WINDOW", 120),
            ack_grace=int_env("NOTIFIER_ACK_GRACE", 60),
            tick=int_env("NOTIFIER_TICK", 15),
            mobile_prefixes=prefixes,
            mobile_device_ids=device_ids,
            state_path=STATE_PATH,
        )


class State:
    """Persistent state: pusher rewrites (for restore) and held pushes."""

    def __init__(self, path: str):
        self.path = path
        self.rewrites = {}  # (app_id, pushkey) -> {"user": mxid, "pusher": original_body}
        self.held = []  # [{id, user, room_id, arrival_ts, deadline, payload, original_url}]
        self._next_id = 1
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log(
                f"warning: cannot read {self.path} ({e!r}) — starting fresh (pushes forwarded immediately)"
            )
            return
        for entry in raw.get("rewrites", []):
            app_id, pushkey = entry.get("app_id"), entry.get("pushkey")
            if app_id and pushkey:
                self.rewrites[(app_id, pushkey)] = {
                    "user": entry.get("user"),
                    "pusher": entry.get("pusher") or {},
                }
        self.held = [it for it in raw.get("held", []) if it.get("payload")]
        ids = [it.get("id", 0) for it in self.held]
        self._next_id = (max(ids) if ids else 0) + 1

    def persist(self):
        # Atomic write so a crash mid-write never corrupts the state file.
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        raw = {
            "rewrites": [
                {
                    "app_id": k[0],
                    "pushkey": k[1],
                    "user": v.get("user"),
                    "pusher": v.get("pusher"),
                }
                for k, v in self.rewrites.items()
            ],
            "held": self.held,
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=2)
        os.replace(tmp, self.path)

    def new_id(self) -> int:
        item_id = self._next_id
        self._next_id += 1
        return item_id


class User:
    """Per-user view: ack tracking via long-poll sync, devices, pushers."""

    def __init__(self, mxid, token, cfg, state, session, snapshot):
        self.mxid = mxid
        self.token = token
        self.cfg = cfg
        self.state = state
        self.session = session
        self.snapshot = snapshot
        self.base = cfg.homeserver_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.acks = {}  # room_id -> last ack ts (ms)
        self.fully_read = {}  # room_id -> m.fully_read event_id
        self.devices = {}  # device_id -> last_seen_ts (ms)
        self.mobile_ids = set()
        self.self_device_id = None
        self.fail_open = True  # unknown mobile ids -> never hold
        self.warned_fail_open = False
        self.filter_id = None
        self.since = None

    # --- activity ----------------------------------------------------------

    def update_ack(self, room_id, ts):
        current = self.acks.get(room_id)
        if current is None or ts > current:
            self.acks[room_id] = ts

    def last_ack(self, room_id):
        return self.acks.get(room_id)

    def active_desktop(self) -> bool:
        """True if a non-mobile, non-self device synced within the hold window."""
        if self.fail_open:
            return False
        excluded = set(self.mobile_ids)
        if self.self_device_id:
            excluded.add(self.self_device_id)
        window = self.cfg.hold_window * 1000
        now = now_ms()
        for device_id, seen in self.devices.items():
            if device_id in excluded:
                continue
            if seen is not None and now - seen <= window:
                return True
        return False

    # --- homeserver API ----------------------------------------------------

    async def ensure_filter(self):
        # Restrict sync to the data the ack tracker needs so a busy account
        # does not download every room's timeline continuously.
        body = {
            "presence": {"not_types": ["*"]},
            "room": {
                "state": {"not_types": ["*"]},
                "timeline": {"limit": 1, "senders": [self.mxid]},
                "ephemeral": {"types": ["m.receipt"]},
                "account_data": {"types": ["m.fully_read"]},
            },
        }
        url = f"{self.base}/_matrix/client/v3/user/{quote(self.mxid, safe='')}/filter"
        async with self.session.post(url, json=body, headers=self.headers) as resp:
            if resp.status == 401:
                raise AuthError(f"user={self.mxid}: filter 401")
            resp.raise_for_status()
            data = await resp.json()
        self.filter_id = data.get("filter_id")

    async def sync_once(self):
        if self.filter_id is None:
            await self.ensure_filter()
        # The notifier must not make the user's account appear online.
        params = {"timeout": str(SYNC_TIMEOUT_MS), "set_presence": "offline"}
        if self.filter_id:
            params["filter"] = self.filter_id
        if self.since:
            params["since"] = self.since
        url = f"{self.base}/_matrix/client/v3/sync"
        async with self.session.get(
            url, params=params, headers=self.headers, timeout=SYNC_CLIENT_TIMEOUT
        ) as resp:
            if resp.status == 401:
                # Token is dead; recreate the filter on the next attempt so a
                # re-minted token does not reuse a filter it cannot access.
                self.filter_id = None
                raise AuthError(f"user={self.mxid}: sync 401")
            if resp.status != 200:
                raise RuntimeError(f"sync HTTP {resp.status}")
            data = await resp.json()
        self._process_sync(data)
        if data.get("next_batch"):
            self.since = data["next_batch"]

    def _process_sync(self, data):
        joined = ((data.get("rooms") or {}).get("join")) or {}
        for room_id, room in joined.items():
            for ev in (room.get("timeline") or {}).get("events") or []:
                if ev.get("sender") == self.mxid and ev.get("origin_server_ts"):
                    self.update_ack(room_id, ev["origin_server_ts"])
            for ev in (room.get("ephemeral") or {}).get("events") or []:
                if ev.get("type") != "m.receipt":
                    continue
                for reads in (ev.get("content") or {}).values():
                    mine = (reads.get("m.read") or {}).get(self.mxid)
                    if mine and mine.get("ts"):
                        self.update_ack(room_id, mine["ts"])
            for ev in (room.get("account_data") or {}).get("events") or []:
                if ev.get("type") != "m.fully_read":
                    continue
                event_id = (ev.get("content") or {}).get("event_id")
                # Only a *change* is an ack; the initial echo is old news.
                if event_id and event_id != self.fully_read.get(room_id):
                    self.fully_read[room_id] = event_id
                    self.update_ack(room_id, now_ms())

    async def whoami(self):
        url = f"{self.base}/_matrix/client/v3/account/whoami"
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status == 401:
                raise AuthError(f"user={self.mxid}: whoami 401")
            resp.raise_for_status()
            data = await resp.json()
        self.self_device_id = data.get("device_id")

    async def get_pushers(self):
        url = f"{self.base}/_matrix/client/v3/pushers"
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status == 401:
                raise AuthError(f"user={self.mxid}: pushers 401")
            resp.raise_for_status()
            data = await resp.json()
        return data.get("pushers") or []

    async def set_pusher(self, body):
        url = f"{self.base}/_matrix/client/v3/pushers/set"
        async with self.session.post(url, json=body, headers=self.headers) as resp:
            if resp.status == 401:
                raise AuthError(f"user={self.mxid}: pushers/set 401")
            resp.raise_for_status()

    async def get_devices(self):
        url = f"{self.base}/_matrix/client/v3/devices"
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status == 401:
                raise AuthError(f"user={self.mxid}: devices 401")
            resp.raise_for_status()
            data = await resp.json()
        self.devices = {
            d["device_id"]: d.get("last_seen_ts")
            for d in data.get("devices") or []
            if d.get("last_seen_ts") is not None
        }

    # --- background loops ---------------------------------------------------

    async def sync_loop(self):
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except AuthError:
                log(
                    f"error: user={self.mxid}: access token rejected (401) — retrying in 60s"
                )
                await asyncio.sleep(60)
            except Exception as e:
                log(f"error: user={self.mxid}: sync failed ({e!r}) — retrying in 5s")
                await asyncio.sleep(5)


class Notifier:
    def __init__(self, cfg, state, session):
        self.cfg = cfg
        self.state = state
        self.session = session
        self.snapshot = {}  # (app_id, pushkey) -> {"user", "original_url"}
        self.users = {
            mxid: User(mxid, token, cfg, state, session, self.snapshot)
            for mxid, token in cfg.users.items()
        }
        # Seed from persisted rewrites so a push arriving before the first tick
        # forwards immediately instead of being dropped.
        for key, entry in state.rewrites.items():
            url = ((entry.get("pusher") or {}).get("data") or {}).get("url")
            if url:
                self.snapshot[key] = {"user": entry.get("user"), "original_url": url}

    def user(self, mxid):
        return self.users.get(mxid)

    def _is_mobile(self, pusher):
        app_id = pusher.get("app_id") or ""
        return any(app_id.startswith(prefix) for prefix in self.cfg.mobile_prefixes)

    # --- tick ---------------------------------------------------------------

    async def tick_loop(self):
        while True:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"error: tick failed ({e!r})")
            await asyncio.sleep(self.cfg.tick)

    async def tick_once(self):
        self.snapshot = {}
        seen = {}  # mxid -> set of mobile pusher keys read this tick
        for user in self.users.values():
            try:
                seen[user.mxid] = await self._tick_user(user)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"error: user={user.mxid}: tick failed ({e!r})")
        self._prune_rewrites(seen)
        await self._evaluate_held()

    async def _tick_user(self, user):
        """Refresh one user; returns the set of mobile pusher keys seen (None if unreadable)."""
        try:
            pushers = await user.get_pushers()
        except Exception as e:
            log(
                f"error: user={user.mxid}: pushers fetch failed ({e!r}) — forwarding everything"
            )
            user.fail_open = True
            return None
        if user.self_device_id is None:
            try:
                await user.whoami()
            except Exception:
                pass  # retried every tick
        mobile = [p for p in pushers if self._is_mobile(p)]
        ids = {
            p.get("org.matrix.msc3881.device_id")
            for p in mobile
            if p.get("org.matrix.msc3881.device_id")
        }
        ids.update(self.cfg.mobile_device_ids)
        if ids:
            if user.fail_open and user.warned_fail_open:
                log(
                    f"info: user={user.mxid}: mobile device ids resolved — holding enabled"
                )
            user.mobile_ids = ids
            user.fail_open = False
        else:
            user.mobile_ids = set()
            user.fail_open = True
            if not user.warned_fail_open:
                log(
                    f"warning: user={user.mxid}: no mobile device ids (MSC3881 off and "
                    "NOTIFIER_MOBILE_DEVICE_IDS empty) — forwarding everything"
                )
                user.warned_fail_open = True
        seen = set()
        for pusher in mobile:
            key = (pusher.get("app_id"), pusher.get("pushkey"))
            if not key[0] or not key[1]:
                continue
            seen.add(key)
            current_url = ((pusher.get("data") or {}).get("url") or "").rstrip("/")
            if current_url != self.cfg.gateway_url.rstrip("/"):
                clean = clean_pusher(pusher)
                # Record the original before rewriting so restore (and fail-open
                # forwarding) always has the real gateway to go back to.
                self.state.rewrites[key] = {"user": user.mxid, "pusher": clean}
                self.state.persist()
                try:
                    await user.set_pusher(rewrite_body(clean, self.cfg.gateway_url))
                    log(f"rewrote pusher user={user.mxid} app_id={key[0]}")
                except Exception as e:
                    log(f"error: user={user.mxid}: pusher rewrite failed ({e!r})")
                    continue
            entry = self.state.rewrites.get(key)
            original_url = ((entry or {}).get("pusher", {}).get("data") or {}).get(
                "url"
            )
            if not original_url:
                log(
                    f"error: user={user.mxid}: no recorded original gateway for {key[0]} — "
                    "its pushes will be dropped"
                )
            self.snapshot[key] = {"user": user.mxid, "original_url": original_url}
        try:
            await user.get_devices()
        except Exception as e:
            log(f"error: user={user.mxid}: devices fetch failed ({e!r})")
        return seen

    def _prune_rewrites(self, seen):
        # Drop records only when we have a fresh pusher list for the owning
        # user — a failed fetch during a Synapse outage must never wipe the
        # original gateways (that would strand rewritten pushers).
        changed = False
        for key, entry in list(self.state.rewrites.items()):
            fresh = seen.get(entry.get("user"))
            if fresh is not None and key not in fresh:
                del self.state.rewrites[key]
                changed = True
        if changed:
            self.state.persist()

    # --- held push lifecycle -------------------------------------------------

    def _current_held(self, mxid, room_id):
        for item in self.state.held:
            if item["user"] == mxid and item.get("room_id") == room_id:
                return item
        return None

    def _remove_held(self, item_id):
        before = len(self.state.held)
        self.state.held = [it for it in self.state.held if it["id"] != item_id]
        if len(self.state.held) != before:
            self.state.persist()

    def _reinsert_held(self, item):
        # Only managed pushes get a retry queue; unmanaged ones are best effort.
        if not item.get("user") or item["user"] not in self.users:
            return
        if self._current_held(item["user"], item.get("room_id")) is not None:
            return  # a newer push for the same room superseded this one
        self.state.held.append(item)
        self.state.persist()

    async def _evaluate_held(self):
        now = now_ms()
        grace = self.cfg.ack_grace * 1000
        for item in list(self.state.held):
            room_id = item.get("room_id")
            user = self.users.get(item.get("user"))
            ack = user.last_ack(room_id) if (user and room_id) else None
            if ack is not None and ack >= item["arrival_ts"] - grace:
                self._remove_held(item["id"])
                log(f"cancel user={item['user']} room={room_id}")
                continue
            if user is None:
                self._remove_held(item["id"])
                log(f"drop user={item['user']} (no longer managed) room={room_id}")
                continue
            if now >= item["deadline"]:
                self._remove_held(item["id"])
                await self._forward(item, "timeout")
            elif not user.active_desktop():
                self._remove_held(item["id"])
                await self._forward(item, "idle")

    async def _forward(self, item, reason):
        delays = (1, 2, 4)
        last_err = "unknown"
        for attempt in range(3):
            try:
                async with self.session.post(
                    item["original_url"], json=item["payload"]
                ) as resp:
                    if 200 <= resp.status < 300:
                        try:
                            body = await resp.json(content_type=None) or {}
                        except Exception:
                            body = {}
                        rejected = set(body.get("rejected") or [])
                        pushkeys = {
                            d.get("pushkey")
                            for d in (item["payload"].get("notification") or {}).get(
                                "devices"
                            )
                            or []
                            if d.get("pushkey")
                        }
                        if rejected & pushkeys:
                            # Gateway says the device is gone; the client will
                            # re-register its pusher, which the tick re-rewrites.
                            log(
                                f"drop user={item['user']} room={item.get('room_id')} (upstream rejected pushkey)"
                            )
                            return
                        log(
                            f"forward user={item['user']} room={item.get('room_id')} reason={reason}"
                        )
                        return
                    last_err = f"HTTP {resp.status}"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_err = repr(e)
            if attempt < 2:
                await asyncio.sleep(delays[attempt])
        log(
            f"error: forward failed user={item['user']} room={item.get('room_id')} ({last_err}) — retrying next tick"
        )
        self._reinsert_held(item)

    def _make_item(self, payload, url, mxid, room_id):
        return {
            "id": self.state.new_id(),
            "user": mxid,
            "room_id": room_id,
            "arrival_ts": now_ms(),
            "deadline": now_ms() + self.cfg.escalate_after * 1000,
            "payload": payload,
            "original_url": url,
        }

    def _hold(self, item, room_id):
        prev = self._current_held(item["user"], room_id)
        if prev is not None:
            self._remove_held(prev["id"])
            log(f"replace user={item['user']} room={room_id} (newer push)")
        self.state.held.append(item)
        self.state.persist()
        log(f"hold user={item['user']} room={room_id}")

    # --- push gateway ---------------------------------------------------------

    async def process_notify(self, payload):
        """Decide hold vs forward for one gateway notification (never raises)."""
        notif = payload.get("notification") or {}
        devices = notif.get("devices") or []
        event_id = notif.get("event_id")
        room_id = notif.get("room_id")
        urls = set()
        url_users = {}
        for device in devices:
            key = (device.get("app_id"), device.get("pushkey"))
            entry = self.snapshot.get(key)
            url = entry.get("original_url") if entry else None
            user = entry.get("user") if entry else None
            if not url:
                fallback = self.state.rewrites.get(key)
                url = ((fallback or {}).get("pusher", {}).get("data") or {}).get("url")
            if url:
                urls.add(url)
                url_users.setdefault(url, set()).add(user)
            else:
                log(
                    f"error: dropping push for {device.get('app_id')} — no recorded gateway (state lost?)"
                )
        if not urls:
            return
        if len(urls) > 1:
            # Synapse POSTs per pusher, so this is unreachable; forward to every
            # known gateway rather than guess.
            for url in urls:
                await self._forward(
                    self._make_item(payload, url, "unknown", room_id), "immediate"
                )
            return
        url = next(iter(urls))
        users = url_users[url]
        mxid = next(iter(users)) if len(users) == 1 else None
        user = self.users.get(mxid) if mxid else None
        item = self._make_item(payload, url, mxid or "unknown", room_id)
        # Fail-open: anything we cannot positively manage goes out immediately.
        # The sub-reason tells operators which guard fired.
        if not event_id:
            await self._forward(item, "immediate:badge")
            return
        if not room_id:
            await self._forward(item, "immediate:no_room")
            return
        if None in users or user is None:
            await self._forward(item, "immediate:unmanaged")
            return
        if user.fail_open:
            await self._forward(item, "immediate:fail_open")
            return
        if not user.active_desktop():
            await self._forward(item, "immediate:no_desktop")
            return
        self._hold(item, room_id)

    async def _process_safely(self, payload):
        try:
            await self.process_notify(payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"error: notify processing failed ({e!r})")

    async def handle_notify(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({}, status=400)
        # Answer immediately; Synapse must never block on our decision.
        asyncio.create_task(self._process_safely(payload))
        return web.json_response({})

    async def handle_healthz(self, request):
        return web.Response(text="ok")

    # --- restore ---------------------------------------------------------------

    async def restore_from_state(self) -> int:
        """Re-POST every recorded original pusher body; exit code 1 on any failure."""
        failed = 0
        for key, entry in list(self.state.rewrites.items()):
            user = self.users.get(entry.get("user"))
            if user is None:
                log(
                    f"error: cannot restore pusher {key[0]}: no token for user {entry.get('user')}"
                )
                failed += 1
                continue
            try:
                await user.set_pusher(entry.get("pusher") or {})
                log(f"restored pusher user={user.mxid} app_id={key[0]}")
            except Exception as e:
                log(f"error: restore failed user={user.mxid} app_id={key[0]} ({e!r})")
                failed += 1
        return 1 if failed else 0


def make_app(notifier):
    app = web.Application()
    app.router.add_post("/_matrix/push/v1/notify", notifier.handle_notify)
    app.router.add_get("/healthz", notifier.handle_healthz)
    return app


async def _run(cfg):
    state = State(cfg.state_path)
    async with ClientSession(timeout=SYNC_CLIENT_TIMEOUT) as session:
        notifier = Notifier(cfg, state, session)
        log(f"managed users: {', '.join(sorted(cfg.users))}")
        runner = web.AppRunner(make_app(notifier))
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", cfg.port)
        await site.start()
        tasks = [
            asyncio.create_task(user.sync_loop()) for user in notifier.users.values()
        ]
        tasks.append(asyncio.create_task(notifier.tick_loop()))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                pass
        await stop.wait()
        log("shutting down: restoring pushers")
        try:
            await notifier.restore_from_state()
        except Exception as e:
            log(f"error: restore failed ({e!r})")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()


async def _restore_cli(cfg):
    state = State(cfg.state_path)
    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
        notifier = Notifier(cfg, state, session)
        return await notifier.restore_from_state()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg = Config.from_env()
    if not cfg.users:
        # No-op on purpose: the compose profile can stay enabled without
        # managing anyone, and plain pushes keep working untouched.
        log("warning: NOTIFIER_USERS is empty — no-op (push notifications unmanaged)")
        return 0
    if "--restore" in argv:
        return asyncio.run(_restore_cli(cfg))
    try:
        asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

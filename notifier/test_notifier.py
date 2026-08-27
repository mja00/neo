"""Unit tests for the notifier: a mocked Synapse plus a captured upstream gateway."""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

import notifier

MXID = "@alice:neo.test"
TOKEN = "mct_test_token"
APP_ID = "io.element.elementx"
PUSHKEY = "pk-mobile-1"
GATEWAY_URL = "http://notifier:8080/_matrix/push/v1/notify"
MATRIX_ORG_URL = "https://matrix.org/_matrix/push/v1/notify"


def notify_payload(
    event_id="$ev", room_id="!room:neo.test", pushkey=PUSHKEY, app_id=APP_ID
):
    return {
        "notification": {
            "event_id": event_id,
            "room_id": room_id,
            "counts": {"unread": 1},
            "prio": "high",
            "devices": [
                {
                    "app_id": app_id,
                    "pushkey": pushkey,
                    "pushkey_ts": 0,
                    "data": {},
                    "tweaks": {},
                }
            ],
        },
    }


class MockSynapse:
    def __init__(self):
        self.fail_pushers = False
        self.pushers = []
        self.set_pusher_calls = []
        self.devices = []
        self.whoami = {"user_id": MXID, "device_id": "NOTIFIERDEV"}
        self.filters = []
        self.sync_requests = []
        self.sync_q = asyncio.Queue()

    def build_app(self):
        app = web.Application()
        app.router.add_get("/_matrix/client/v3/pushers", self.handle_get_pushers)
        app.router.add_post("/_matrix/client/v3/pushers/set", self.handle_set_pusher)
        app.router.add_get("/_matrix/client/v3/devices", self.handle_devices)
        app.router.add_get("/_matrix/client/v3/account/whoami", self.handle_whoami)
        app.router.add_post("/_matrix/client/v3/user/{uid}/filter", self.handle_filter)
        app.router.add_get("/_matrix/client/v3/sync", self.handle_sync)
        return app

    async def handle_get_pushers(self, request):
        if self.fail_pushers:
            return web.json_response(
                {"errcode": "M_UNKNOWN", "error": "boom"}, status=503
            )
        return web.json_response({"pushers": self.pushers})

    async def handle_set_pusher(self, request):
        self.set_pusher_calls.append(await request.json())
        return web.json_response({})

    async def handle_devices(self, request):
        return web.json_response({"devices": self.devices})

    async def handle_whoami(self, request):
        return web.json_response(self.whoami)

    async def handle_filter(self, request):
        self.filters.append(await request.json())
        return web.json_response({"filter_id": "f-1"})

    async def handle_sync(self, request):
        self.sync_requests.append(dict(request.query))
        return web.json_response(await self.sync_q.get())


class Upstream:
    def __init__(self):
        self.calls = []

    def build_app(self):
        app = web.Application()
        app.router.add_post("/notify", self.handle)
        return app

    async def handle(self, request):
        self.calls.append(await request.json())
        return web.json_response({})


def mobile_pusher(url):
    return {
        "pushkey": PUSHKEY,
        "kind": "http",
        "app_id": APP_ID,
        "app_display_name": "Element X",
        "device_display_name": "iPhone",
        "lang": "en",
        "data": {"url": url, "format": "event_id_only"},
        "org.matrix.msc3881.enabled": True,
        "org.matrix.msc3881.device_id": "MOBILE1",
    }


def rewrite_entry(world, url=None):
    world.state.rewrites[(APP_ID, PUSHKEY)] = {
        "user": MXID,
        "pusher": {
            "kind": "http",
            "app_id": APP_ID,
            "app_display_name": "Element X",
            "device_display_name": "iPhone",
            "pushkey": PUSHKEY,
            "lang": "en",
            "data": {"url": url or world.upstream_url, "format": "event_id_only"},
        },
    }
    world.state.persist()


def setup_active(world, pusher_url=GATEWAY_URL):
    """Mobile pusher plus an actively-syncing desktop device."""
    world.mock.pushers = [mobile_pusher(pusher_url)]
    world.mock.devices = [
        {"device_id": "MOBILE1", "last_seen_ts": notifier.now_ms() - 500},
        {"device_id": "DESKTOP1", "last_seen_ts": notifier.now_ms()},
    ]


@pytest.fixture
async def world(tmp_path, monkeypatch):
    mock = MockSynapse()
    up = Upstream()
    mock_server = TestServer(mock.build_app())
    up_client = TestClient(TestServer(up.build_app()))
    await mock_server.start_server()
    await up_client.start_server()
    cfg = notifier.Config(
        users={MXID: TOKEN},
        homeserver_url=str(mock_server.make_url("/")),
        gateway_url=GATEWAY_URL,
        state_path=str(tmp_path / "state.json"),
        escalate_after=10.0,
        hold_window=5.0,
        ack_grace=2.0,
        tick=1.0,
    )
    state = notifier.State(cfg.state_path)
    session = ClientSession()
    n = notifier.Notifier(cfg, state, session)
    clock = [1_000_000.0]
    monkeypatch.setattr(notifier, "now_ms", lambda: int(clock[0] * 1000))
    try:
        yield SimpleNamespace(
            mock=mock,
            up=up,
            cfg=cfg,
            state=state,
            n=n,
            session=session,
            clock=clock,
            upstream_url=str(up_client.make_url("/notify")),
            user=n.users[MXID],
        )
    finally:
        await session.close()
        await up_client.close()
        await mock_server.close()


async def test_hold_when_desktop_active(world):
    rewrite_entry(world)
    setup_active(world)
    await world.n.tick_once()
    assert not world.user.fail_open

    payload = notify_payload()
    await world.n.process_notify(payload)

    assert len(world.state.held) == 1
    assert world.state.held[0]["payload"] == payload
    assert world.up.calls == []


async def test_notifier_sync_does_not_affect_presence(world):
    world.mock.sync_q.put_nowait({"next_batch": "b1"})

    await world.user.sync_once()

    assert world.mock.sync_requests == [
        {"timeout": "30000", "set_presence": "offline", "filter": "f-1"}
    ]


async def test_cancel_on_same_room_receipt_within_grace(world):
    rewrite_entry(world)
    setup_active(world)
    await world.n.tick_once()
    await world.n.process_notify(notify_payload())
    held_arrival = world.state.held[0]["arrival_ts"]

    world.mock.sync_q.put_nowait(
        {
            "next_batch": "b1",
            "rooms": {
                "join": {
                    "!room:neo.test": {
                        "ephemeral": {
                            "events": [
                                {
                                    "type": "m.receipt",
                                    "content": {
                                        "$e1": {
                                            "m.read": {
                                                MXID: {"ts": held_arrival + 1000}
                                            }
                                        }
                                    },
                                },
                            ]
                        }
                    }
                }
            },
        }
    )
    await world.user.sync_once()
    await world.n.tick_once()

    assert world.state.held == []
    assert world.up.calls == []


async def test_forward_at_deadline(world):
    rewrite_entry(world)
    setup_active(world)
    await world.n.tick_once()
    payload = notify_payload()
    await world.n.process_notify(payload)

    world.clock[0] += 11.0  # past escalate_after=10
    await world.n.tick_once()

    assert world.state.held == []
    assert world.up.calls == [payload]


async def test_forward_when_desktop_goes_stale(world):
    rewrite_entry(world)
    setup_active(world)
    await world.n.tick_once()
    payload = notify_payload()
    await world.n.process_notify(payload)

    world.clock[0] += 6.0  # beyond hold_window=5, before the deadline
    await world.n.tick_once()

    assert world.state.held == []
    assert world.up.calls == [payload]


async def test_badge_only_push_forwarded_immediately(world):
    rewrite_entry(world)
    setup_active(world)
    await world.n.tick_once()
    payload = {
        "notification": {
            "counts": {"unread": 0},
            "prio": "low",
            "devices": [
                {
                    "app_id": APP_ID,
                    "pushkey": PUSHKEY,
                    "pushkey_ts": 0,
                    "data": {},
                    "tweaks": {},
                }
            ],
        },
    }
    await world.n.process_notify(payload)

    assert world.state.held == []
    assert world.up.calls == [payload]


async def test_unknown_device_fail_open(world):
    # No snapshot entry (no tick ran); the rewrites fallback supplies the URL,
    # and an unmanaged device is forwarded, never blocked.
    world.state.rewrites[(APP_ID, PUSHKEY)] = {
        "user": MXID,
        "pusher": {
            "kind": "http",
            "app_id": APP_ID,
            "pushkey": PUSHKEY,
            "data": {"url": world.upstream_url},
        },
    }
    payload = notify_payload()
    await world.n.process_notify(payload)

    assert world.state.held == []
    assert world.up.calls == [payload]


async def test_later_push_replaces_held_for_same_room(world):
    rewrite_entry(world)
    setup_active(world)
    await world.n.tick_once()
    await world.n.process_notify(notify_payload(event_id="$e1"))
    await world.n.process_notify(notify_payload(event_id="$e2"))

    assert len(world.state.held) == 1
    assert world.state.held[0]["payload"]["notification"]["event_id"] == "$e2"
    assert world.up.calls == []


async def test_rewrite_post_body_correctness(world):
    setup_active(world, pusher_url=MATRIX_ORG_URL)  # not yet rewritten
    await world.n.tick_once()

    assert len(world.mock.set_pusher_calls) == 1
    body = world.mock.set_pusher_calls[0]
    assert body["kind"] == "http"
    assert body["app_id"] == APP_ID
    assert body["app_display_name"] == "Element X"
    assert body["device_display_name"] == "iPhone"
    assert body["pushkey"] == PUSHKEY
    assert body["lang"] == "en"
    assert body["data"]["url"] == GATEWAY_URL
    assert body["data"]["format"] == "event_id_only"
    assert "append" not in body
    assert "org.matrix.msc3881.device_id" not in body

    stored = world.state.rewrites[(APP_ID, PUSHKEY)]
    assert stored["user"] == MXID
    assert stored["pusher"]["data"]["url"] == MATRIX_ORG_URL
    assert world.n.snapshot[(APP_ID, PUSHKEY)]["original_url"] == MATRIX_ORG_URL


async def test_restore_cli_reposts_original_body(world):
    rewrite_entry(world, url=MATRIX_ORG_URL)

    code = await world.n.restore_from_state()

    assert code == 0
    assert len(world.mock.set_pusher_calls) == 1
    body = world.mock.set_pusher_calls[0]
    assert body["data"]["url"] == MATRIX_ORG_URL
    assert "append" not in body


async def test_state_survives_restart(world):
    item = {
        "id": 1,
        "user": MXID,
        "room_id": "!room:neo.test",
        "arrival_ts": notifier.now_ms() - 60_000,
        "deadline": notifier.now_ms() - 30_000,
        "payload": notify_payload(),
        "original_url": world.upstream_url,
    }
    world.state.held.append(item)
    world.state.persist()

    # Fresh process: new State/Notifier loaded from the same file.
    state2 = notifier.State(world.cfg.state_path)
    n2 = notifier.Notifier(world.cfg, state2, world.session)
    await n2.tick_once()

    assert state2.held == []
    assert world.up.calls == [item["payload"]]


async def test_outage_does_not_prune_rewrites(world):
    # A Synapse outage must not wipe recorded gateways: without them, pushes
    # to already-rewritten pushers have nowhere to go.
    rewrite_entry(world)
    setup_active(world)
    await world.n.tick_once()
    assert (APP_ID, PUSHKEY) in world.state.rewrites

    world.mock.fail_pushers = True
    await world.n.tick_once()

    assert (APP_ID, PUSHKEY) in world.state.rewrites


async def test_prune_removes_when_pusher_gone(world):
    rewrite_entry(world)
    setup_active(world)
    await world.n.tick_once()
    assert (APP_ID, PUSHKEY) in world.state.rewrites

    world.mock.pushers = []  # client deleted/re-registered the pusher
    await world.n.tick_once()

    assert (APP_ID, PUSHKEY) not in world.state.rewrites

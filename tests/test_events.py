"""EventHub — the push channel that replaced gateway-status and thumbnail
polling. Pure asyncio, deterministic."""

from __future__ import annotations

from srv.events import EventHub


async def test_hub_fans_out_to_all_subscribers():
    hub = EventHub()
    a, b = hub.subscribe(), hub.subscribe()
    hub.emit({"type": "gateway", "n": 1})
    assert await a.get() == {"type": "gateway", "n": 1}
    assert await b.get() == {"type": "gateway", "n": 1}


async def test_unsubscribe_stops_delivery():
    hub = EventHub()
    a = hub.subscribe()
    hub.unsubscribe(a)
    hub.emit({"x": 1})
    assert a.empty()
    assert hub.subscriber_count == 0


async def test_full_queue_drops_without_blocking_producer():
    # A stuck consumer must never stall the gateway. Excess events drop for
    # that subscriber only; emit() always returns immediately.
    hub = EventHub(max_queue=1)
    a = hub.subscribe()
    hub.emit({"n": 1})
    hub.emit({"n": 2})  # dropped — queue full, no exception
    assert await a.get() == {"n": 1}
    assert a.empty()


async def test_close_wakes_subscribers_even_when_queue_is_full():
    hub = EventHub(max_queue=1)
    subscriber = hub.subscribe()
    hub.emit({"old": "event"})
    hub.close()
    assert await subscriber.get() is EventHub.CLOSE

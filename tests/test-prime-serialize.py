from __future__ import annotations
import importlib.util, os, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gw", ROOT / "reasonix-native-gateway.py")
gw = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(gw)


def expect(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")


def test_first_n_lanes_serialize_rest_parallel():
    # The first N lanes of a prefix family must run one-at-a-time (each waits for
    # the previous to finish + grace) so the prefix is persisted before the burst;
    # lane N+1 onward run free. acquire_serial_slot(key, n) returns True if this
    # caller holds a serial slot (must release_serial_slot when done), False if it
    # is past the serial window and may run in parallel.
    os.environ["CLAUDE_REASONIX_GATEWAY_PRIME_SERIAL"] = "3"
    key = "famKEY-serialize"
    # reset state for this key
    gw.reset_prime_state(key)
    slots = [gw.acquire_serial_slot(key) for _ in range(5)]
    # first 3 are serial (True), last 2 are parallel (False)
    expect(slots[:3] == [True, True, True], f"first 3 serial, got {slots}")
    expect(slots[3:] == [False, False], f"rest parallel, got {slots}")


def test_serial_disabled_when_zero():
    os.environ["CLAUDE_REASONIX_GATEWAY_PRIME_SERIAL"] = "0"
    key = "famKEY-off"
    gw.reset_prime_state(key)
    slots = [gw.acquire_serial_slot(key) for _ in range(4)]
    expect(all(s is False for s in slots), "serial=0 disables -> all parallel")
    os.environ["CLAUDE_REASONIX_GATEWAY_PRIME_SERIAL"] = "3"


def test_serial_lock_is_mutually_exclusive():
    # A serial-slot holder takes the per-key serial lock; the next holder must
    # block until release. We prove the lock exists and serializes.
    os.environ["CLAUDE_REASONIX_GATEWAY_PRIME_SERIAL"] = "3"
    key = "famKEY-mutex"
    gw.reset_prime_state(key)
    order = []
    gw.acquire_serial_slot(key)
    lock = gw.serial_lock_for(key)
    lock.acquire()
    released = {"v": False}

    def second():
        lock.acquire()  # must block until first releases
        order.append("second-got-lock")
        lock.release()

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.1)
    expect(order == [], "second blocked while first holds lock")
    lock.release()
    t.join(timeout=2)
    expect(order == ["second-got-lock"], "second proceeded after release")


if __name__ == "__main__":
    test_first_n_lanes_serialize_rest_parallel()
    test_serial_disabled_when_zero()
    test_serial_lock_is_mutually_exclusive()
    print("PASS: prime serialize")

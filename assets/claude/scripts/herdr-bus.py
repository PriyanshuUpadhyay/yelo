#!/usr/bin/env python3
"""Session-bound pane pub/sub over a rename-based file bus. Signaling only:
completion truth stays with agent-handoff.py verify. Delivery is set
difference (events minus per-subscriber markers), never a cursor watermark,
so publication order and consumption order are independent."""
import argparse, json, math, os, re, sys, time, uuid

IDENT = re.compile(r"^[a-z0-9-]{1,32}$")
NAME = re.compile(r"^(\d{19})\.([a-z0-9-]{1,32})\.([a-z0-9-]{1,32})\.([0-9a-f]{8})\.json$")

def bus_dirs():
    root = os.environ.get("HERDR_BUS_DIR")
    if not root:
        ws = os.environ.get("HERDR_WORKSPACE_ID")
        if not ws:
            sys.exit("herdr-bus: set HERDR_BUS_DIR or HERDR_WORKSPACE_ID")
        root = os.path.join("/tmp", f"herdr-bus-{os.getuid()}", ws)
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
    d = {k: os.path.join(root, k) for k in ("tmp", "events", "cursors", "watch")}
    for p in d.values():
        os.makedirs(p, exist_ok=True)
    return {"root": root, **d}

def sanitize(value):
    out = re.sub(r"[^a-z0-9-]", "-", (value or "").lower())[:32].strip("-")
    if not IDENT.match(out):
        sys.exit(f"herdr-bus: unusable identifier {value!r}")
    return out

def emit(from_, kind, ref=None, data=None):
    d = bus_dirs()
    ts = time.time_ns()
    body = json.dumps({"ts": ts, "from": from_, "kind": kind,
                       "ref": ref, "data": data}, separators=(",", ":"))
    if len(body) > 4096:
        sys.exit("herdr-bus: payload over 4KB — write a side file and pass --ref")
    name = f"{ts:019d}.{from_}.{kind}.{uuid.uuid4().hex[:8]}.json"
    tmp = os.path.join(d["tmp"], name)
    with open(tmp, "w") as fh:
        fh.write(body)
    # No fsync: rename atomicity gives process-crash safety; power-loss
    # durability is deliberately not promised (artifacts are the durable truth).
    os.rename(tmp, os.path.join(d["events"], name))
    print(f"herdr-bus: root={d['root']}", file=sys.stderr)
    return name

def marker_dir(d, sub):
    p = os.path.join(d["cursors"], sub + ".d")
    os.makedirs(p, exist_ok=True)
    return p

def unread(d, sub):
    events = {n for n in os.listdir(d["events"]) if NAME.match(n)}
    return sorted(events - set(os.listdir(marker_dir(d, sub))))

def mark(d, sub, name):
    p = marker_dir(d, sub)
    tmp = os.path.join(p, f"{name}.tmp.{os.getpid()}")
    open(tmp, "w").close()
    os.rename(tmp, os.path.join(p, name))

def scan(sub, consume=False, out=sys.stdout):
    d = bus_dirs()
    delivered = []
    for name in unread(d, sub):
        with open(os.path.join(d["events"], name)) as fh:
            body = json.loads(fh.read())
        body["name"] = name
        out.write(json.dumps(body, separators=(",", ":")) + "\n")
        out.flush()
        if consume:
            mark(d, sub, name)  # after output: crash here redelivers, never loses
        delivered.append(name)
    return delivered

_LEASE = None  # lease this process owns; released on every exit path

def write_lease(d, sub, ttl):
    path = os.path.join(d["watch"], sub + ".lease")
    tmp = path + f".tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump({"pid": os.getpid(), "ttl_s": ttl}, fh)
    os.rename(tmp, path)
    return path

def release_lease():
    """Drop our own lease so an exited watcher never looks live to the chair.

    Only ours: a replacement watcher may already have taken the name over, and
    deleting its lease would let a third watcher arm alongside it.
    """
    if not _LEASE:
        return
    try:
        with open(_LEASE) as fh:
            if json.load(fh).get("pid") != os.getpid():
                return
        os.unlink(_LEASE)
    except (Exception, SystemExit):
        pass  # a surviving stale lease is no worse than never releasing at all

def finish(code, reason, n_unread, new, via):
    release_lease()
    print(json.dumps({"reason": reason, "cursor_at_wake": n_unread,
                      "new": new, "via": via}))
    sys.exit(code)

def watch_error(detail, via="poll"):
    release_lease()
    print(json.dumps({"reason": "error", "cursor_at_wake": None,
                      "new": [], "via": via, "detail": detail}))
    sys.exit(1)  # 2 is reserved for a quiet TTL; errors must never claim it

def positive_seconds(flag, value):
    if not math.isfinite(value) or value <= 0:
        sys.exit(f"herdr-bus: {flag} must be a finite number > 0, got {value!r}")
    return value

def run_watch(subscriber, ttl, tick):
    """Single exit funnel for the watch subcommand: every path out of here
    prints exactly one final JSON line, and only a quiet TTL exits 2."""
    try:
        watch(sanitize(subscriber), positive_seconds("--ttl", ttl),
              positive_seconds("--tick", tick))
    except SystemExit as err:
        if isinstance(err.code, int):
            raise  # finish()/watch_error() already printed the final line
        watch_error(str(err.code))  # sys.exit("message") from sanitize/bus_dirs
    except BaseException as err:
        watch_error(f"{type(err).__name__}: {err}")

def watch(sub, ttl, tick):
    global _LEASE
    via = "poll"
    d = bus_dirs()
    lease_path = _LEASE = write_lease(d, sub, ttl)
    kq = ev = None
    try:
        import select
        kq = select.kqueue()
        dfd = os.open(d["events"], os.O_RDONLY)
        ev = select.kevent(dfd, filter=select.KQ_FILTER_VNODE,
                           flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                           fflags=select.KQ_NOTE_WRITE)
        kq.control([ev], 0, 0)  # register BEFORE the first scan (arm-then-scan)
        via = "kqueue"
    except (ImportError, AttributeError, OSError):
        kq = None
    deadline = time.monotonic() + ttl
    while True:
        new = unread(d, sub)
        if new:
            finish(0, "event", len(new), new, via)
        if time.monotonic() >= deadline:
            finish(2, "ttl", 0, [], via)
        os.utime(lease_path)  # heartbeat: metadata-only, never an append
        remaining = max(0.05, min(tick, deadline - time.monotonic()))
        if kq:
            kq.control(None, 1, remaining)
        else:
            time.sleep(remaining)

def pid_alive(pid):
    """Whether a lease's owner still exists — a SIGKILLed watcher never released."""
    if isinstance(pid, bool) or not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal
    except OSError:
        return False
    return True

def doctor(gc):
    d = bus_dirs()
    now = time.time()
    events = [n for n in os.listdir(d["events"]) if NAME.match(n)]
    report = {"lease_age_s": {}, "lease_ttl_s": {}, "lease_alive": {}, "unread": {},
              "tmp_orphans": 0, "events": len(events), "gc_events": 0}
    # every per-entry stat/unlink is guarded: an entry can be renamed or
    # unlinked between the listdir and the stat on a live bus, and doctor is a
    # report, not a gate — it skips what vanished rather than dying on it
    for f in os.listdir(d["watch"]):
        if f.endswith(".lease"):
            p = os.path.join(d["watch"], f)
            try:
                # one descriptor for both: a lease replaced mid-read would
                # otherwise pair one generation's age with another's ttl, and
                # the chair's arming rule compares exactly those two
                with open(p) as fh:
                    age = round(now - os.fstat(fh.fileno()).st_mtime, 1)
                    try:
                        body = json.load(fh)
                    except (json.JSONDecodeError, OSError):
                        body = {}
            except (FileNotFoundError, OSError):
                continue
            if not isinstance(body, dict):
                body = {}
            report["lease_age_s"][f[:-6]] = age
            report["lease_ttl_s"][f[:-6]] = body.get("ttl_s")
            report["lease_alive"][f[:-6]] = pid_alive(body.get("pid"))
    subs = [f[:-2] for f in os.listdir(d["cursors"]) if f.endswith(".d")]
    for s in subs:
        report["unread"][s] = len(unread(d, s))
    for f in os.listdir(d["tmp"]):
        p = os.path.join(d["tmp"], f)
        try:
            if now - os.stat(p).st_mtime > 3600:
                report["tmp_orphans"] += 1
                if gc:
                    os.unlink(p)
        except (FileNotFoundError, OSError):
            continue
    for s in subs:  # mark() leftovers: inert for delivery, reclaimed only here
        md = marker_dir(d, s)
        for f in os.listdir(md):
            p = os.path.join(md, f)
            try:
                if ".tmp." in f and now - os.stat(p).st_mtime > 3600:
                    report["tmp_orphans"] += 1
                    if gc:
                        os.unlink(p)
            except (FileNotFoundError, OSError):
                continue
    reclaimed = set()
    for n in events:
        p = os.path.join(d["events"], n)
        try:
            if now - os.stat(p).st_mtime > 7 * 86400:
                report["gc_events"] += 1
                if gc:
                    os.unlink(p)
                    reclaimed.add(n)
        except (FileNotFoundError, OSError):
            continue
    if gc:
        # A marker dies only if its event is absent from an events/ listing
        # taken AFTER the marker listing AND the marker itself is older than
        # the event horizon. Race-free by construction: event names are unique
        # forever (ns timestamp + uuid), so once a name is absent from events/
        # it is absent permanently — absence is monotonic, and re-checking it
        # after the marker listing only makes the test more conservative. The
        # sole racy entity is a FRESH marker written mid-run by a live scan,
        # and the age floor excludes every fresh marker by construction. An
        # orphan from a late-subscriber interleaving survives at most until it
        # ages past the horizon and is reclaimed by a later gc run: bounded
        # eventual cleanup, never a deleted valid marker.
        # Markers of events this run just unlinked are exempt from the age
        # floor: that event is provably gone, so the marker cannot be suppress-
        # ing any future delivery. Removing them in the same run is BEST-EFFORT
        # by contract — the closing probe below shrinks the window to the point
        # where only a mark() landing after the probe escapes, and that one is
        # left to the 7-day reclamation, which is the actual guarantee.
        markers = []
        for f in os.listdir(d["cursors"]):
            if not f.endswith(".d"):
                continue
            md = os.path.join(d["cursors"], f)
            try:
                markers += [(md, m) for m in os.listdir(md) if ".tmp." not in m]
            except OSError:
                continue
        live = set(os.listdir(d["events"]))
        for md, m in markers:
            p = os.path.join(md, m)
            try:
                if m not in live and (m in reclaimed
                                      or now - os.stat(p).st_mtime > 7 * 86400):
                    os.unlink(p)
            except (FileNotFoundError, OSError):
                continue
        for n in reclaimed:
            # probe by constructed path against a fresh cursors listing: no
            # marker listing to be stale, so a mark() that landed after the
            # sweep's listing — even into a subscriber dir created since — is
            # still reached
            for f in os.listdir(d["cursors"]):
                if not f.endswith(".d"):
                    continue
                try:
                    os.unlink(os.path.join(d["cursors"], f, n))
                except (FileNotFoundError, OSError):
                    continue
    print(json.dumps(report))

def main():
    ap = argparse.ArgumentParser(prog="herdr-bus")
    # unrecognized args surface on the top-level parser, so route those to the
    # watch finalizer too when watch is the command being run
    ap_error = ap.error
    ap.error = lambda message: (watch_error(f"watch: {message}")
                                if sys.argv[1:2] == ["watch"] else ap_error(message))
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit")
    e.add_argument("--from", dest="from_", required=True)
    e.add_argument("--kind", required=True)
    e.add_argument("--ref")
    e.add_argument("--data")
    s = sub.add_parser("scan")
    s.add_argument("--subscriber", required=True)
    s.add_argument("--consume", action="store_true")
    w = sub.add_parser("watch")
    w.error = lambda message: watch_error(f"watch: {message}")  # not argparse's exit 2
    w.add_argument("--subscriber", required=True)
    w.add_argument("--ttl", type=float, default=900.0)
    w.add_argument("--tick", type=float, default=1.0)
    dr = sub.add_parser("doctor")
    dr.add_argument("--gc", action="store_true")
    a = ap.parse_args()
    if a.cmd == "emit":
        data = json.loads(a.data) if a.data else None
        print(emit(sanitize(a.from_), sanitize(a.kind), a.ref, data))
    elif a.cmd == "scan":
        scan(sanitize(a.subscriber), a.consume)
    elif a.cmd == "watch":
        run_watch(a.subscriber, a.ttl, a.tick)
    elif a.cmd == "doctor":
        doctor(a.gc)

if __name__ == "__main__":
    main()

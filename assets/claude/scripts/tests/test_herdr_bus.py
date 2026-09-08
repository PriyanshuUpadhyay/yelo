import contextlib, importlib.util, io, json, os, shutil, subprocess, tempfile, time, unittest
BUS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "herdr-bus.py")

_spec = importlib.util.spec_from_file_location("herdr_bus", BUS)
bus = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bus)

def run(env, *args):
    return subprocess.run(["python3", BUS, *args], env={**os.environ, **env},
                          capture_output=True, text=True)

class TestEmitScan(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {"HERDR_BUS_DIR": self.dir}

    def test_emit_name_grammar_body_and_stderr_root(self):
        r = run(self.env, "emit", "--from", "coder-1", "--kind", "done", "--ref", "/tmp/x.json")
        self.assertEqual(r.returncode, 0, r.stderr)
        name = r.stdout.strip()
        self.assertRegex(name, r"^\d{19}\.coder-1\.done\.[0-9a-f]{8}\.json$")
        self.assertIn(self.dir, r.stderr)
        with open(os.path.join(self.dir, "events", name)) as fh:
            body = json.load(fh)
        self.assertEqual(body["from"], "coder-1")
        self.assertEqual(str(body["ts"]), name.split(".")[0].lstrip("0"))  # one time_ns feeds both
        self.assertEqual(os.listdir(os.path.join(self.dir, "tmp")), [])

    def test_workspace_default_uses_the_cross_sandbox_temp_root(self):
        workspace = f"test-{os.getpid()}-{time.time_ns()}"
        env = {"HERDR_WORKSPACE_ID": workspace}
        env["HERDR_BUS_DIR"] = ""
        r = run(env, "emit", "--from", "coder-1", "--kind", "done")
        self.assertEqual(r.returncode, 0, r.stderr)
        root = os.path.join("/tmp", f"herdr-bus-{os.getuid()}", workspace)
        self.addCleanup(shutil.rmtree, root, True)
        self.assertIn(root, r.stderr)
        self.assertEqual(os.stat(root).st_mode & 0o777, 0o700)
        self.assertEqual(len(os.listdir(os.path.join(root, "events"))), 1)

    def test_from_is_sanitized_not_rejected(self):
        r = run(self.env, "emit", "--from", "sdd.implementer/task3", "--kind", "done")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(".sdd-implementer-task3.done.", r.stdout)

    def test_scan_consume_and_redelivery_semantics(self):
        for kind in ("done", "blocked"):
            run(self.env, "emit", "--from", "w", "--kind", kind)
        r = run(self.env, "scan", "--subscriber", "chair", "--consume")
        self.assertEqual([json.loads(l)["kind"] for l in r.stdout.splitlines()],
                         ["done", "blocked"])
        self.assertEqual(run(self.env, "scan", "--subscriber", "chair").stdout, "")
        # marker removal simulates a crash-before-mark: the event redelivers
        marked = os.listdir(os.path.join(self.dir, "cursors", "chair.d"))
        os.unlink(os.path.join(self.dir, "cursors", "chair.d", sorted(marked)[0]))
        r2 = run(self.env, "scan", "--subscriber", "chair")
        self.assertEqual(len(r2.stdout.splitlines()), 1)

    def test_mark_publishes_via_tmp_rename(self):
        # Direct test of mark(): the old open(final, "w").close() records no
        # rename at all, so only tmp->rename publication satisfies this.
        name = f"{1:019d}.w.done.deadbeef.json"
        final = os.path.join(self.dir, "cursors", "chair.d", name)
        renames, real_rename = [], os.rename
        prev = os.environ.get("HERDR_BUS_DIR")
        os.environ["HERDR_BUS_DIR"] = self.dir
        def spy(src, dst):
            renames.append((src, dst))
            return real_rename(src, dst)
        os.rename = spy
        try:
            bus.mark(bus.bus_dirs(), "chair", name)
        finally:
            os.rename = real_rename
            if prev is None:
                os.environ.pop("HERDR_BUS_DIR", None)
            else:
                os.environ["HERDR_BUS_DIR"] = prev
        self.assertTrue(any(".tmp." in src and dst == final for src, dst in renames), renames)
        self.assertTrue(os.path.exists(final))
        self.assertEqual([n for n in os.listdir(os.path.dirname(final)) if ".tmp." in n], [])

    def test_slow_publisher_is_never_skipped(self):
        # The scalar-cursor killer (council change #1): an event whose name sorts
        # BELOW already-consumed names must still be delivered.
        run(self.env, "emit", "--from", "w", "--kind", "done")
        run(self.env, "scan", "--subscriber", "chair", "--consume")
        old = f"{1:019d}.late.done.deadbeef.json"
        with open(os.path.join(self.dir, "tmp", old), "w") as fh:
            fh.write(json.dumps({"ts": 1, "from": "late", "kind": "done",
                                 "ref": None, "data": None}))
        os.rename(os.path.join(self.dir, "tmp", old),
                  os.path.join(self.dir, "events", old))
        r = run(self.env, "scan", "--subscriber", "chair")
        self.assertEqual(json.loads(r.stdout.splitlines()[0])["from"], "late")


import threading

class TestWatch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {"HERDR_BUS_DIR": self.dir}

    def _watch_bg(self, *extra):
        res = {}
        def go():
            res["r"] = run(self.env, "watch", "--subscriber", "chair",
                           "--ttl", "10", "--tick", "0.2", *extra)
        t = threading.Thread(target=go); t.start()
        return t, res

    def test_wakes_on_event_and_does_not_consume(self):
        t, res = self._watch_bg()
        time.sleep(0.8)
        run(self.env, "emit", "--from", "coder-1", "--kind", "done")
        t.join(timeout=8); self.assertFalse(t.is_alive())
        r = res["r"]; self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        final = json.loads(r.stdout.splitlines()[-1])
        self.assertEqual(final["reason"], "event")
        self.assertEqual(len(final["new"]), 1)
        self.assertIn(final["via"], ("kqueue", "poll"))
        self.assertEqual(len(run(self.env, "scan", "--subscriber", "chair")
                             .stdout.splitlines()), 1)  # still unconsumed

    def test_preexisting_unconsumed_event_wakes_immediately(self):
        run(self.env, "emit", "--from", "w", "--kind", "done")
        t0 = time.monotonic()
        r = run(self.env, "watch", "--subscriber", "chair", "--ttl", "10", "--tick", "5")
        self.assertEqual(r.returncode, 0)
        self.assertLess(time.monotonic() - t0, 3)  # no tick wait on first scan

    def test_ttl_exit_when_quiet_and_lease_heartbeat(self):
        # the lease is inspected WHILE the watcher runs: it is released on exit,
        # so a post-run read would find nothing
        res = {}
        def go():
            res["r"] = run(self.env, "watch", "--subscriber", "chair",
                           "--ttl", "3", "--tick", "0.2")
        t = threading.Thread(target=go); t.start()
        path = os.path.join(self.dir, "watch", "chair.lease")
        for _ in range(50):
            if os.path.exists(path):
                break
            time.sleep(0.05)
        with open(path) as fh:
            lease = json.load(fh)
        self.assertEqual(lease["ttl_s"], 3.0)  # persisted, not null
        mtime = os.stat(path).st_mtime
        time.sleep(0.6)
        self.assertGreater(os.stat(path).st_mtime, mtime)  # heartbeat
        t.join(timeout=10); self.assertFalse(t.is_alive())
        r = res["r"]
        self.assertEqual(r.returncode, 2)
        self.assertEqual(json.loads(r.stdout.splitlines()[-1])["reason"], "ttl")

    def test_error_path_still_prints_final_json(self):
        r = subprocess.run(["python3", BUS, "watch", "--subscriber", "chair"],
                           env={k: v for k, v in os.environ.items()
                                if k not in ("HERDR_BUS_DIR", "HERDR_WORKSPACE_ID")},
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout.splitlines()[-1])["reason"], "error")

    def _assert_watch_error(self, r):
        # exit 2 is reserved for a quiet TTL, so errors must never use it
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 1, r.stdout)  # exactly one final line
        final = json.loads(lines[-1])
        self.assertEqual(final["reason"], "error")
        self.assertEqual(set(final) >= {"cursor_at_wake", "new", "via", "detail"}, True)

    def test_malformed_ttl_argument_is_an_error_not_a_ttl_exit(self):
        self._assert_watch_error(run(self.env, "watch", "--subscriber", "chair",
                                     "--ttl", "nope"))

    def test_unrecognized_watch_flag_is_an_error(self):
        # unrecognized args are raised by the TOP-LEVEL parser, not the subparser
        self._assert_watch_error(run(self.env, "watch", "--subscriber", "chair", "--bogus"))

    def test_empty_subscriber_is_an_error(self):
        self._assert_watch_error(run(self.env, "watch", "--subscriber", ""))

    def test_unusable_bus_root_prints_final_json(self):
        self._assert_watch_error(run({"HERDR_BUS_DIR": "/dev/null"},
                                     "watch", "--subscriber", "chair"))

    def test_non_finite_ttl_is_rejected_promptly(self):
        # bounded: an unvalidated NaN deadline is never reached, so the watcher
        # would otherwise hang the whole suite rather than fail it
        try:
            r = subprocess.run(["python3", BUS, "watch", "--subscriber", "chair",
                                "--ttl", "nan", "--tick", "0.05"],
                               env={**os.environ, **self.env},
                               capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("watch --ttl nan never exited: NaN deadline is unreachable")
        self._assert_watch_error(r)


class TestDoctor(unittest.TestCase):
    def test_reports_and_gc(self):
        d = tempfile.mkdtemp(); env = {"HERDR_BUS_DIR": d}
        run(env, "emit", "--from", "w", "--kind", "done")
        orphan = os.path.join(d, "tmp", "0" * 19 + ".w.done.deadbeef.json")
        with open(orphan, "w") as fh:
            fh.write("{}")
        os.utime(orphan, (time.time() - 7200,) * 2)
        stale = "1".zfill(19) + ".w.done.cafecafe.json"
        with open(os.path.join(d, "tmp", stale), "w") as fh:
            fh.write("{}")
        os.rename(os.path.join(d, "tmp", stale), os.path.join(d, "events", stale))
        os.utime(os.path.join(d, "events", stale), (time.time() - 8 * 86400,) * 2)
        run(env, "scan", "--subscriber", "chair", "--consume")
        rep = json.loads(run(env, "doctor", "--gc").stdout)
        self.assertEqual(rep["events"], 2)
        self.assertEqual(rep["tmp_orphans"], 1)
        self.assertEqual(rep["gc_events"], 1)
        self.assertFalse(os.path.exists(orphan))
        self.assertFalse(os.path.exists(os.path.join(d, "events", stale)))
        self.assertFalse(os.path.exists(os.path.join(d, "cursors", "chair.d", stale)))

    def test_gc_sweeps_aged_markers_whose_event_is_gone(self):
        # end state of any GC/late-subscriber interleaving: a marker exists for
        # an event that is no longer in events/. Reclaimed once it ages past
        # the same 7-day horizon used for event GC.
        d = tempfile.mkdtemp(); env = {"HERDR_BUS_DIR": d}
        run(env, "emit", "--from", "w", "--kind", "done")
        run(env, "scan", "--subscriber", "chair", "--consume")
        late_md = os.path.join(d, "cursors", "late.d")
        os.makedirs(late_md)
        orphan = os.path.join(late_md, "1".zfill(19) + ".w.done.cafecafe.json")
        open(orphan, "w").close()
        os.utime(orphan, (time.time() - 8 * 86400,) * 2)
        # a fresh orphan is indistinguishable from a marker a live scan is
        # writing right now, so the mtime floor must spare it this round
        fresh_orphan = os.path.join(late_md, "2".zfill(19) + ".w.done.beadfeed.json")
        open(fresh_orphan, "w").close()
        rep = json.loads(run(env, "doctor", "--gc").stdout)
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(fresh_orphan))
        chair_md = os.path.join(d, "cursors", "chair.d")
        self.assertEqual(len(os.listdir(chair_md)), 1)  # live marker untouched
        self.assertEqual(rep["unread"]["chair"], 0)

    def test_fresh_marker_for_a_live_event_survives_gc(self):
        d = tempfile.mkdtemp(); env = {"HERDR_BUS_DIR": d}
        run(env, "emit", "--from", "w", "--kind", "done")
        run(env, "scan", "--subscriber", "chair", "--consume")
        md = os.path.join(d, "cursors", "chair.d")
        marker = os.listdir(md)[0]
        rep = json.loads(run(env, "doctor", "--gc").stdout)
        self.assertTrue(os.path.exists(os.path.join(md, marker)))
        self.assertEqual(rep["unread"]["chair"], 0)
        self.assertEqual(len(run(env, "scan", "--subscriber", "chair")
                             .stdout.splitlines()), 0)  # no redelivery

    def test_marker_written_during_the_sweep_survives(self):
        # the reviewer's trigger: a scan consumes an event AFTER the sweep has
        # taken its events listing. Only the mtime floor can save that marker.
        d = tempfile.mkdtemp()
        prev = os.environ.get("HERDR_BUS_DIR")
        os.environ["HERDR_BUS_DIR"] = d
        real_listdir, injected = os.listdir, []
        def spy(path, *a, **kw):
            names = real_listdir(path, *a, **kw)
            # inject after EVERY events listing rather than guessing which one
            # the sweep takes: whichever it was, an emit + scan --consume
            # landed immediately after it returned
            if str(path) == os.path.join(d, "events") and len(injected) < 5:
                name = f"{len(injected) + 2:019d}.w.done.abadcafe.json"
                injected.append(name)
                open(os.path.join(d, "events", name), "w").close()
                open(os.path.join(d, "cursors", "chair.d", name), "w").close()
            return names
        buf = io.StringIO()
        try:
            dirs = bus.bus_dirs()
            bus.mark(dirs, "chair", f"{3:019d}.w.done.0badf00d.json")
            os.listdir = spy
            with contextlib.redirect_stdout(buf):
                bus.doctor(True)
        finally:
            os.listdir = real_listdir
            if prev is None:
                os.environ.pop("HERDR_BUS_DIR", None)
            else:
                os.environ["HERDR_BUS_DIR"] = prev
        self.assertTrue(injected, "spy never fired")
        for name in injected:  # every fresh marker must survive the sweep
            self.assertTrue(os.path.exists(os.path.join(d, "cursors", "chair.d", name)),
                            f"swept a valid marker written mid-run: {name}")
        self.assertEqual(len(bus.unread(dirs, "chair")), 0)  # so no redelivery

    def test_lease_age_and_ttl_come_from_one_open(self):
        # age must be fstat'd on the descriptor json.load reads, or a lease
        # replaced mid-read yields an age and a ttl from different generations
        d = tempfile.mkdtemp()
        prev = os.environ.get("HERDR_BUS_DIR")
        os.environ["HERDR_BUS_DIR"] = d
        stat_paths, real_stat = [], os.stat
        def spy(path, *a, **kw):
            stat_paths.append(str(path))
            return real_stat(path, *a, **kw)
        buf = io.StringIO()
        try:
            bus.write_lease(bus.bus_dirs(), "chair", 900.0)
            os.stat = spy
            with contextlib.redirect_stdout(buf):
                bus.doctor(False)
        finally:
            os.stat = real_stat
            if prev is None:
                os.environ.pop("HERDR_BUS_DIR", None)
            else:
                os.environ["HERDR_BUS_DIR"] = prev
        rep = json.loads(buf.getvalue())
        self.assertEqual(rep["lease_ttl_s"]["chair"], 900.0)
        self.assertIsNotNone(rep["lease_age_s"]["chair"])
        self.assertNotIn(os.path.join(d, "watch", "chair.lease"), stat_paths)

    def test_probe_removes_marker_written_after_the_sweep_listing(self):
        # a scan consumes an expiring event after the sweep has listed markers:
        # the sweep cannot see that marker, so the closing probe must reach it
        # by constructed path — including in a subscriber dir that did not
        # exist when the sweep ran
        d = tempfile.mkdtemp()
        prev = os.environ.get("HERDR_BUS_DIR")
        os.environ["HERDR_BUS_DIR"] = d
        stale = f"{1:019d}.w.done.cafecafe.json"
        real_listdir, cursor_listings, injections = os.listdir, [], []
        def spy(path, *a, **kw):
            names = real_listdir(path, *a, **kw)
            if str(path) == os.path.join(d, "cursors"):
                cursor_listings.append(True)  # 1: subs scan, 2: the sweep's
            # the sweep lists cursors, then markers, then events — so the first
            # events listing after the 2nd cursors listing is strictly past the
            # marker listing the sweep will act on
            if (str(path) == os.path.join(d, "events")
                    and len(cursor_listings) >= 2 and not injections):
                injections.append(True)
                late_md = os.path.join(d, "cursors", "late.d")
                os.makedirs(late_md, exist_ok=True)
                open(os.path.join(late_md, stale), "w").close()
            return names
        buf = io.StringIO()
        try:
            dirs = bus.bus_dirs()
            with open(os.path.join(dirs["events"], stale), "w") as fh:
                fh.write("{}")
            os.utime(os.path.join(dirs["events"], stale), (time.time() - 8 * 86400,) * 2)
            os.listdir = spy
            with contextlib.redirect_stdout(buf):
                bus.doctor(True)
        finally:
            os.listdir = real_listdir
            if prev is None:
                os.environ.pop("HERDR_BUS_DIR", None)
            else:
                os.environ["HERDR_BUS_DIR"] = prev
        self.assertTrue(injections, "spy never fired")
        self.assertEqual(json.loads(buf.getvalue())["gc_events"], 1)
        self.assertFalse(os.path.exists(os.path.join(d, "cursors", "late.d", stale)))

    def test_dangling_entries_never_traceback(self):
        # a broken symlink stats exactly like an entry unlinked between the
        # listdir and the stat — one per scan loop: tmp, events, markers, leases
        d = tempfile.mkdtemp(); env = {"HERDR_BUS_DIR": d}
        run(env, "emit", "--from", "w", "--kind", "done")
        run(env, "scan", "--subscriber", "chair", "--consume")
        md = os.path.join(d, "cursors", "chair.d")
        marker = os.listdir(md)[0]
        for path in (os.path.join(d, "tmp", "0" * 19 + ".w.done.deadbeef.json"),
                     os.path.join(d, "events", "0" * 19 + ".w.done.beefdead.json"),
                     os.path.join(md, marker + ".tmp.777"),
                     os.path.join(d, "watch", "ghost.lease")):
            os.symlink(os.path.join(d, "no-such-target"), path)
        stale = os.path.join(d, "tmp", "0" * 19 + ".w.done.feedface.json")
        with open(stale, "w") as fh:
            fh.write("{}")
        os.utime(stale, (time.time() - 7200,) * 2)
        r = run(env, "doctor", "--gc")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rep = json.loads(r.stdout)
        self.assertEqual(rep["tmp_orphans"], 1)  # skipped the dangling ones, kept going
        self.assertFalse(os.path.exists(stale))
        self.assertNotIn("ghost", rep["lease_age_s"])
        self.assertNotIn("ghost", rep["lease_ttl_s"])

    def test_gc_reclaims_stale_marker_tmp_files(self):
        # crash-between-write-and-rename leftovers from mark(): inert for
        # delivery, but nothing else ever reclaims them
        d = tempfile.mkdtemp(); env = {"HERDR_BUS_DIR": d}
        run(env, "emit", "--from", "w", "--kind", "done")
        run(env, "scan", "--subscriber", "chair", "--consume")
        md = os.path.join(d, "cursors", "chair.d")
        marker = os.listdir(md)[0]
        old = os.path.join(md, marker + ".tmp.999")
        recent = os.path.join(md, marker + ".tmp.998")
        for p in (old, recent):
            open(p, "w").close()
        os.utime(old, (time.time() - 7200,) * 2)
        rep = json.loads(run(env, "doctor", "--gc").stdout)
        self.assertEqual(rep["tmp_orphans"], 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))  # a mark() may be in flight
        self.assertTrue(os.path.exists(os.path.join(md, marker)))
        self.assertEqual(rep["unread"]["chair"], 0)  # markers still suppress delivery


class TestWatchLeaseLifecycle(unittest.TestCase):
    """An exited watcher must never look live: its lease would block the re-arm."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = {"HERDR_BUS_DIR": self.dir}
        self.lease = os.path.join(self.dir, "watch", "chair.lease")

    def _watch_bg(self, ttl):
        res = {}
        def go():
            res["r"] = run(self.env, "watch", "--subscriber", "chair",
                           "--ttl", ttl, "--tick", "0.2")
        t = threading.Thread(target=go); t.start()
        for _ in range(50):  # wait for the lease so the race is not the test
            if os.path.exists(self.lease):
                break
            time.sleep(0.05)
        return t, res

    def _rearm_permitted(self):
        rep = json.loads(run(self.env, "doctor").stdout)
        return ("chair" not in rep["lease_age_s"]
                or not rep["lease_alive"].get("chair", False))

    def test_event_exit_permits_rearm(self):
        t, res = self._watch_bg("10")
        run(self.env, "emit", "--from", "w", "--kind", "done")
        t.join(timeout=10); self.assertFalse(t.is_alive())
        self.assertEqual(res["r"].returncode, 0, res["r"].stderr)
        self.assertTrue(self._rearm_permitted(), "event exit left a live-looking lease")

    def test_ttl_exit_permits_rearm(self):
        r = run(self.env, "watch", "--subscriber", "chair", "--ttl", "1", "--tick", "0.2")
        self.assertEqual(r.returncode, 2)
        self.assertTrue(self._rearm_permitted(), "ttl exit left a live-looking lease")

    def test_error_exit_permits_rearm(self):
        import shutil
        t, res = self._watch_bg("10")
        shutil.rmtree(os.path.join(self.dir, "events"))  # unread() fails mid-loop
        t.join(timeout=10); self.assertFalse(t.is_alive())
        self.assertEqual(res["r"].returncode, 1, res["r"].stdout)
        self.assertTrue(self._rearm_permitted(), "error exit left a live-looking lease")

    def test_exiting_watcher_never_deletes_a_newer_lease(self):
        t, res = self._watch_bg("10")
        tmp = self.lease + ".tmp.test"
        with open(tmp, "w") as fh:  # a replacement watcher takes over the lease
            json.dump({"pid": os.getpid(), "ttl_s": 900.0}, fh)
        os.rename(tmp, self.lease)
        run(self.env, "emit", "--from", "w", "--kind", "done")
        t.join(timeout=10); self.assertFalse(t.is_alive())
        self.assertTrue(os.path.exists(self.lease), "older watcher deleted a newer lease")
        with open(self.lease) as fh:
            self.assertEqual(json.load(fh)["pid"], os.getpid())
        rep = json.loads(run(self.env, "doctor").stdout)
        self.assertTrue(rep["lease_alive"]["chair"])  # this test process is alive

    def test_lease_alive_is_false_for_a_dead_pid(self):
        os.makedirs(os.path.join(self.dir, "watch"), exist_ok=True)
        with open(self.lease, "w") as fh:
            json.dump({"pid": 2 ** 22, "ttl_s": 900.0}, fh)  # no such process
        rep = json.loads(run(self.env, "doctor").stdout)
        self.assertIn("chair", rep["lease_age_s"])  # fresh mtime, looks live by age
        self.assertFalse(rep["lease_alive"]["chair"])
        self.assertEqual(set(rep["lease_alive"]), set(rep["lease_age_s"]))


DISPATCH_KW = dict(seat_id="coder-1", run_id="r", workflow="wf", round_id=1, attempt=1,
                   input_digest="0" * 64, run_dir="/tmp/rd", prompt="do the thing")

def load_agent_handoff():
    import sys
    # Its sibling, the same way BUS above resolves herdr-bus.py. The old path reached into
    # ~/dotfiles, which no longer carries this file: jello installs it now.
    spec = importlib.util.spec_from_file_location(
        "ah", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "agent-handoff.py"))
    ah = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = ah  # dataclass creation resolves via sys.modules
    spec.loader.exec_module(ah)
    return ah

def emit_line(text):
    """The one line a worker runs to ring the doorbell.

    Located by the env prefix it must start with, never by the command name: that name
    moved to a PATH shim once already, and a locator that finds nothing raises before any
    assertion runs, so three tests here reported a stale name and a real regression with
    the same traceback.
    """
    lines = [l for l in text.splitlines() if l.startswith("HERDR_BUS_DIR=")]
    assert len(lines) == 1, f"expected exactly one emit line, found {lines!r}"
    return lines[0]


class TestDispatchWiring(unittest.TestCase):
    def setUp(self):
        prev = {name: os.environ.get(name)
                for name in ("HERDR_BUS_DIR", "HERDR_BUS_COMMAND")}
        def restore():
            for name, value in prev.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.addCleanup(restore)

    def test_dispatch_text_contains_emit_with_literal_bus_root(self):
        ah = load_agent_handoff()
        os.environ["HERDR_BUS_DIR"] = "/tmp/bus-under-test"
        text = ah.dispatch_text_for_test(seat_id="coder-1", run_id="r", workflow="wf",
                                         round_id=1, attempt=1, input_digest="0" * 64,
                                         run_dir="/tmp/rd", prompt="do the thing")
        self.assertIn("emit", emit_line(text))
        self.assertIn("HERDR_BUS_DIR=/tmp/bus-under-test", text)  # literal, not $VAR
        self.assertIn("--from coder-1 --kind done", text)
        self.assertLess(text.index("publish"), text.index("emit"))  # emit only after publish

    def test_relative_bus_root_is_interpolated_absolute(self):
        # a worker runs the line from its own cwd, so a relative root would
        # resolve to a different tree entirely
        import shlex
        ah = load_agent_handoff()
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(d)
        self.addCleanup(os.chdir, cwd)
        os.environ["HERDR_BUS_DIR"] = "relative/bus"
        text = ah.dispatch_text(**DISPATCH_KW)
        root = shlex.split(emit_line(text))[0].split("=", 1)[1]
        self.assertTrue(os.path.isabs(root), root)
        self.assertTrue(root.endswith("relative/bus"), root)

    def test_bus_root_with_spaces_survives_the_shell(self):
        import shlex
        # Pinned to this checkout's script: the shipped default is a PATH shim, and a
        # test that needs ~/.local/bin populated measures the machine, not the wiring.
        os.environ["HERDR_BUS_COMMAND"] = BUS
        ah = load_agent_handoff()
        spaced = os.path.join(tempfile.mkdtemp(), "herdr bus")
        os.environ["HERDR_BUS_DIR"] = spaced
        text = ah.dispatch_text(**DISPATCH_KW)
        parts = shlex.split(emit_line(text))
        self.assertEqual(parts[0], f"HERDR_BUS_DIR={spaced}")
        self.assertEqual(parts[1], BUS)
        self.assertEqual(parts[2], "emit")
        # and it actually runs: the placeholder is the only part a worker edits
        cmd = emit_line(text).replace("--ref <artifact path>", "--ref /tmp/artifact.json")
        r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(os.listdir(os.path.join(spaced, "events"))), 1)

    def test_unloadable_helper_degrades_to_publish_only(self):
        ah = load_agent_handoff()
        d = tempfile.mkdtemp()
        stub = os.path.join(d, "herdr-bus.py")
        with open(stub, "w") as fh:
            fh.write("def broken(:\n")  # SyntaxError escapes a narrow except tuple
        ah.HERDR_BUS = stub
        os.environ["HERDR_BUS_DIR"] = d
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            text = ah.dispatch_text(**DISPATCH_KW)
        self.assertIn("publish", text)
        self.assertNotIn("herdr-bus.py", text)
        self.assertIn("unusable", err.getvalue())

import json, multiprocessing, os, random, signal, subprocess, tempfile, time, unittest
BUS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "herdr-bus.py")

EMITTERS, PER_EMITTER, VICTIMS = 50, 20, 16

def emitter(busdir, idx, count):
    os.setsid()  # own process group, so a killpg takes the in-flight emit child too
    env = {**os.environ, "HERDR_BUS_DIR": busdir}
    for i in range(count):
        payload = json.dumps({"seq": i, "pad": "x" * random.randint(500, 3000)})
        r = subprocess.run(["python3", BUS, "emit", "--from", f"w{idx}",
                            "--kind", "done", "--data", payload],
                           env=env, capture_output=True, text=True)
        if r.returncode != 0:  # a broken emit must fail the test, not empty it
            raise RuntimeError(f"emit rc={r.returncode}: {r.stderr.strip()}")

def event_names(busdir):
    try:
        return os.listdir(os.path.join(busdir, "events"))
    except FileNotFoundError:  # nothing published yet; let the assertions report it
        return []

def published_counts(busdir):
    counts = {}
    for n in event_names(busdir):
        who = n.split(".")[1]
        counts[who] = counts.get(who, 0) + 1
    return counts

class TestTorture(unittest.TestCase):
    def test_concurrent_emit_with_kills_no_torn_reads_no_dupes_no_skips(self):
        busdir = tempfile.mkdtemp()
        env = {**os.environ, "HERDR_BUS_DIR": busdir}
        procs = [multiprocessing.Process(target=emitter, args=(busdir, i, PER_EMITTER))
                 for i in range(EMITTERS)]
        for p in procs: p.start()
        victims = random.sample(range(EMITTERS), VICTIMS)
        # kill only once several victims are provably mid-run: the crash arm is
        # then exercised by construction instead of by scheduling luck
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            c = published_counts(busdir)
            if sum(1 for i in victims if 0 < c.get(f"w{i}", 0) < PER_EMITTER) >= 3:
                break
            if all(not p.is_alive() for p in procs):
                break  # nothing left to kill; the exitcode assertions diagnose why
            time.sleep(0.05)
        killed = set()
        for i in victims:
            if procs[i].pid is None:
                continue
            try:  # pgid == pid after setsid, so this reaches a writer mid-write
                os.killpg(procs[i].pid, signal.SIGKILL)
                killed.add(i)
            except (ProcessLookupError, PermissionError):
                pass
        seen = []
        for _ in range(400):  # reader consumes THROUGHOUT the chaos
            out = subprocess.run(["python3", BUS, "scan", "--subscriber", "chair",
                                  "--consume"], env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            for line in out.stdout.splitlines():
                seen.append(json.loads(line)["name"])   # torn JSON would raise here
            if all(not p.is_alive() for p in procs): break
            time.sleep(0.05)
        for p in procs: p.join()
        final = subprocess.run(["python3", BUS, "scan", "--subscriber", "chair",
                                "--consume"], env=env, capture_output=True, text=True)
        seen += [json.loads(l)["name"] for l in final.stdout.splitlines()]

        # the harness must not be able to pass on an empty bus
        for i, p in enumerate(procs):
            if i not in killed:
                self.assertEqual(p.exitcode, 0, f"emitter w{i} exited {p.exitcode}")
        self.assertTrue(killed, "no emitter was killed")
        events = event_names(busdir)
        self.assertGreaterEqual(len(events), (EMITTERS - VICTIMS) * PER_EMITTER,
                                "surviving emitters did not publish their events")

        # the crash-during-publish arm was actually reached
        self.assertTrue(any(procs[i].exitcode == -signal.SIGKILL for i in killed),
                        "no emitter died by SIGKILL")
        counts = published_counts(busdir)
        partial = [i for i in killed
                   if 0 < counts.get(f"w{i}", 0) < PER_EMITTER]
        self.assertTrue(partial, f"no killed emitter died mid-publish: {counts}")

        # a writer killed between write and rename leaves tmp debris that must
        # never have become visible nor been delivered
        for n in os.listdir(os.path.join(busdir, "tmp")):
            self.assertNotIn(n, events, "partially written event became visible")
            self.assertNotIn(n, seen, "partially written event was delivered")

        self.assertEqual(len(seen), len(set(seen)), "duplicate delivery")
        self.assertEqual(sorted(seen), sorted(events),
                         "event visible in events/ but never delivered (skip)")
        for n in events:  # every visible event is byte-complete
            with open(os.path.join(busdir, "events", n)) as fh:
                json.load(fh)

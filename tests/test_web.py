"""Tests for the dashboard's non-HTTP logic (python3 -m unittest discover -s tests)."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spring_migrate_web as web  # noqa: E402


class SchemaTest(unittest.TestCase):
    def test_schema_follows_cli(self):
        schema = web.option_schema()
        dests = {o["dest"]: o for o in schema["migrate"]}
        self.assertEqual(dests["target"]["kind"], "choice")
        self.assertIn("4.1", dests["target"]["choices"])
        self.assertEqual(dests["skip_tests"]["kind"], "flag")
        self.assertEqual(dests["internal_group"]["kind"], "list")
        self.assertEqual(dests["retries"]["kind"], "int")
        self.assertNotIn("report_dir", dests)       # set by the dashboard
        self.assertNotIn("interactive", dests)      # needs a terminal

    def test_argv(self):
        schema = web.option_schema()
        argv = web.build_argv("migrate", "/p", {"target": "3.5", "skip_tests": True, "dry_run": False, "retries": 2,
                                                "internal_group": ["com.acme", "org.corp"], "build_args": "-s s.xml"},
                              schema, Path("/r"), Path("/o"))
        a = argv[2:]
        self.assertEqual(a[:3], [str(web.TOOL), "migrate", "/p"])
        self.assertIn("--skip-tests", a)
        self.assertNotIn("--dry-run", a)
        self.assertEqual(a[a.index("--target") + 1], "3.5")
        self.assertNotIn("--retries", a)           # equals the default
        self.assertEqual([a[i + 1] for i, x in enumerate(a) if x == "--internal-group"], ["com.acme", "org.corp"])
        self.assertEqual(a[a.index("--build-args") + 1], "-s s.xml")
        self.assertEqual(a[-2:], ["--report-dir", "/r"])
        scan = web.build_argv("scan", "/p", {"json": False}, schema, Path("/r"), Path("/o/scan.md"))
        self.assertEqual(scan[-2:], ["--out", "/o/scan.md"])


class TrackerTest(unittest.TestCase):
    def test_stages(self):
        t = web.StageTracker()
        for line in """[preflight] JDK 21
[baseline] build + tests on the ORIGINAL code
[baseline] OK
[deps] checking 2 parent/BOM/dependency reference(s)
[stage 1-boot-2.7-latest] Spring Boot 2.7.13 -> latest 2.7.x
[stage 1-boot-2.7-latest] build + tests
[stage 1-boot-2.7-latest] build OK
[stage 2-boot-3.5] Spring Boot 3.5
  legacy: pom.xml: something
  fixer: Dockerfile: something
[stage 2-boot-3.5] attempt 1 failed (exit 1); retrying in 30s
[stage 2-boot-3.5] build + tests
[stage 2-boot-3.5] BUILD/TESTS FAILED
[done] report: /r/MIGRATION_REPORT.md
[done] stopped at stage 2-boot-3.5. Automated changes are committed""".splitlines():
            t.feed(line)
        t.close("stopped")
        st = {s["key"]: s for s in t.steps}
        self.assertEqual([s["key"] for s in t.steps], ["preflight", "baseline", "deps", "1-boot-2.7-latest", "2-boot-3.5"])
        self.assertEqual(st["baseline"]["status"], "ok")
        self.assertEqual(st["1-boot-2.7-latest"]["status"], "ok")
        self.assertEqual(st["2-boot-3.5"]["status"], "failed")
        self.assertEqual(st["2-boot-3.5"]["changes"], 2)
        self.assertEqual(st["2-boot-3.5"]["title"], "Spring Boot 3.5")
        self.assertEqual(t.summary["stopped_at"], "2-boot-3.5")

    def test_error_marks_step(self):
        t = web.StageTracker()
        t.feed("[preflight] JDK 21")
        t.feed("error: Not a git repository.")
        self.assertEqual(t.steps[0]["status"], "failed")
        self.assertEqual(t.summary["error"], "Not a git repository.")


class ServerTest(unittest.TestCase):
    """Runs the real HTTP server with a fake tool, to check queueing, streaming state and persistence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "proj"
        self.project.mkdir()
        (self.project / "pom.xml").write_text("<project/>")
        fake = root / "fake_tool.py"
        fake.write_text("import sys, time\nprint('[preflight] JDK 21', flush=True)\nprint('[stage 1-x] Step one', flush=True)\n"
                        "time.sleep(0.3)\nprint('[stage 1-x] build OK', flush=True)\nprint('[done] report: x', flush=True)\n")
        self.old_tool = web.TOOL
        web.TOOL = fake
        self.store = web.JobStore(root / "state", parallel=1)
        web.Handler.store, web.Handler.token = self.store, None
        self.srv = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        web.TOOL = self.old_tool
        self.tmp.cleanup()

    def call(self, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_queue_runs_jobs_one_after_the_other(self):
        jobs = self.call("/api/jobs", {"command": "migrate", "projects": [str(self.project)] * 2, "options": {}, "copy": False})
        self.assertEqual(len(jobs), 2)
        deadline = time.time() + 20
        while time.time() < deadline:
            state = {j["id"]: j for j in self.call("/api/jobs")}
            self.assertLessEqual(sum(1 for j in state.values() if j["status"] == "running"), 1)
            if all(j["status"] == "succeeded" for j in state.values()):
                break
            time.sleep(0.1)
        detail = self.call(f"/api/jobs/{jobs[0]['id']}")
        self.assertEqual(detail["status"], "succeeded")
        self.assertEqual([s["status"] for s in detail["steps"]], ["ok", "ok"])
        self.assertIn("[stage 1-x] build OK", detail["lines"])
        # history survives a restart of the dashboard
        again = web.JobStore(self.store.root, parallel=1)
        self.assertEqual(again.jobs[jobs[0]["id"]].d["status"], "succeeded")

    def test_cross_origin_post_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.call("/api/jobs", {"projects": [str(self.project)]}, {"Origin": "https://evil.example"})
        self.assertEqual(e.exception.code, 403)


if __name__ == "__main__":
    unittest.main()

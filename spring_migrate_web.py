#!/usr/bin/env python3
"""
spring-migrate web dashboard - run, queue and watch migrations in the browser.

    python3 spring_migrate_web.py                  # http://127.0.0.1:8765
    python3 spring_migrate_web.py --port 9000 --parallel 2

Everything runs on this machine: the dashboard starts `spring_migrate.py` as a subprocess per job (with your Maven,
JDK and settings.xml) and streams its output. Jobs, logs and reports are kept in ~/.spring-migrate-web (--state-dir),
so the history survives restarts. The server binds to 127.0.0.1; it executes commands on your machine, so only expose
it with --host together with the access token it prints.

Python 3.9+, standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
TOOL = HERE / "spring_migrate.py"
sys.path.insert(0, str(HERE))
import spring_migrate as sm  # noqa: E402

COMMANDS = ("migrate", "scan", "deps", "update")
TERMINAL = ("succeeded", "failed", "stopped", "cancelled", "error")
# options the dashboard sets itself or that need a terminal
HIDDEN_OPTIONS = {"help", "path", "report_dir", "interactive", "out"}


# --------------------------------------------------------------------------- #
# Option schema, derived from the CLI so the form always matches spring_migrate.py
# --------------------------------------------------------------------------- #
OPTION_GROUPS = {
    "target": "Target", "java": "Target", "java_home": "Build", "build_args": "Build", "retries": "Build",
    "dry_run": "Run", "skip_tests": "Run", "skip_baseline": "Run", "allow_dirty": "Run", "no_git": "Run", "force": "Run",
    "openjpa": "Migration steps", "no_openjpa": "Migration steps", "no_legacy": "Migration steps",
    "no_trailing_slash_compat": "Migration steps", "ci_profile": "Build", "smoke_test": "Run", "smoke_profiles": "Run", "no_strict_gate": "Build", "db2_platform": "Migration steps", "no_properties_migrator": "Migration steps",
    "dependency_plan": "Company dependencies", "internal_group": "Company dependencies", "no_dependency_detection": "Company dependencies", "refresh_dependency_versions": "Company dependencies",
    "offline": "Build", "rewrite_spring_version": "Advanced", "rewrite_plugin_version": "Advanced", "extra_recipe": "Advanced",
    "json": "Run", "skip_frontend": "Build", "format_goal": "Build", "no_format": "Build", "rewrite_frontend": "Advanced", "rewrite_args": "Advanced", "rewrite_no_fork": "Advanced",
}


def option_schema() -> dict:
    ap = sm.build_parser()
    sub = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
    out = {}
    for cmd in COMMANDS:
        opts = []
        for act in sub.choices[cmd]._actions:
            if act.dest in HIDDEN_OPTIONS or not act.option_strings:
                continue
            kind = ("flag" if isinstance(act, argparse._StoreTrueAction) else
                    "list" if isinstance(act, argparse._AppendAction) else
                    "choice" if act.choices else
                    "int" if act.type is int else "text")
            opts.append({"dest": act.dest, "flag": max(act.option_strings, key=len), "kind": kind,
                         "choices": [str(c) for c in act.choices] if act.choices else None,
                         "default": act.default if act.default is not argparse.SUPPRESS else None,
                         "help": (act.help or "").replace("%%", "%"), "group": OPTION_GROUPS.get(act.dest, "Advanced")})
        out[cmd] = opts
    return out


def build_argv(command: str, path: str, options: dict, schema: dict, report_dir: Path, out_file: Path) -> list[str]:
    argv = [sys.executable, "-u", str(TOOL), command, path]
    for o in schema[command]:
        v = options.get(o["dest"])
        if o["kind"] == "flag":
            if v:
                argv.append(o["flag"])
        elif o["kind"] == "list":
            items = v if isinstance(v, list) else [x for x in re.split(r"[\n,]", v or "") if x.strip()]
            for item in items:
                argv += [o["flag"], str(item).strip()]
        elif v not in (None, "") and str(v) != str(o["default"]):
            argv += [o["flag"], str(v)]
    if command in ("migrate", "update"):
        argv += ["--report-dir", str(report_dir)]
    elif command == "scan":
        argv += ["--out", str(out_file)]
    elif command == "deps":
        argv += ["--out", str(out_file)]
    return argv


# --------------------------------------------------------------------------- #
# Stage tracking from the tool's output
# --------------------------------------------------------------------------- #
class StageTracker:
    """Turns spring_migrate.py output lines into a list of steps with status and timing."""

    def __init__(self):
        self.steps: list[dict] = []
        self.current: dict | None = None
        self.summary = {}

    def _step(self, key: str, title: str) -> dict:
        for s in self.steps:
            if s["key"] == key:
                return s
        s = {"key": key, "title": title, "status": "running", "started": time.time(), "ended": None, "changes": 0, "notes": []}
        if self.current and self.current["status"] == "running" and self.current is not s:
            self._finish(self.current, "ok")
        self.steps.append(s)
        self.current = s
        return s

    @staticmethod
    def _finish(s: dict, status: str):
        if s["status"] in ("running", "retrying"):
            s["status"] = status
            s["ended"] = time.time()

    def feed(self, line: str):
        line = line.rstrip("\n")
        if line.startswith("error: "):
            self.summary["error"] = line[7:]
            if self.current:
                self.current["notes"].append(line[7:])
                self._finish(self.current, "failed")
            return
        m = re.match(r"\[(preflight|baseline|deps|git|done|dry-run|check)\]\s*(.*)$", line)
        st = re.match(r"\[stage (\S+)\]\s*(.*)$", line)
        if st:
            key, rest = st.groups()
            s = self._step(key, rest if not any(s["key"] == key for s in self.steps) else "")
            if not s["title"]:
                s["title"] = rest
            if re.search(r"build OK", rest):
                self._finish(s, "ok")
            elif re.search(r"BUILD/TESTS FAILED|OpenRewrite failed|could not download", rest):
                self._finish(s, "failed")
            elif re.search(r"attempt \d+ failed", rest):
                s["status"] = "retrying"
                s["notes"].append(rest)
            elif "re-running build" in rest:
                s["notes"].append(rest)
            return
        if m:
            tag, rest = m.groups()
            if tag in ("preflight", "baseline", "deps", "dry-run"):
                title = {"preflight": "Pre-flight checks", "baseline": "Baseline build + tests (original code)",
                         "deps": "Company dependencies", "dry-run": "Dry run (patch only)"}[tag]
                s = self._step(tag, title)
                if tag == "baseline" and rest.startswith("OK"):
                    self._finish(s, "ok")
                elif tag == "baseline" and "FAILED" in rest:
                    self._finish(s, "failed")
                elif tag == "preflight" and "BLOCKERS" in rest:
                    self._finish(s, "failed")
                elif rest and tag != "baseline":
                    s["notes"].append(rest)
            elif tag == "done":
                if self.current:
                    self._finish(self.current, "ok")
                if rest.startswith("report:"):
                    self.summary["report"] = rest.split(":", 1)[1].strip()
                if "stopped at stage" in rest:
                    self.summary["stopped_at"] = re.search(r"stopped at stage (\S+)", rest).group(1).rstrip(".:")
            elif tag == "check":
                self.summary.setdefault("checks", []).append(rest)
            return
        if self.current and re.match(r"\s+(legacy|fixer|bump|deps|openjpa|props-migrator):", line):
            self.current["changes"] += 1

    def close(self, status: str):
        for s in self.steps:
            self._finish(s, "failed" if status in ("failed", "error") and s is self.current else
                         ("cancelled" if status == "cancelled" else "ok"))


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, store: "JobStore", data: dict):
        self.store = store
        self.d = data
        self.lines: list[str] = []
        self.tracker = StageTracker()
        self.proc: subprocess.Popen | None = None
        self.cond = threading.Condition()
        log = self.dir / "output.log"
        if log.exists():
            self.lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
            for l in self.lines:
                self.tracker.feed(l)
            if self.d["status"] in TERMINAL:
                self.tracker.close(self.d["status"])

    @property
    def id(self) -> str:
        return self.d["id"]

    @property
    def dir(self) -> Path:
        return self.store.root / "jobs" / self.id

    def save(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "job.json.tmp"
        tmp.write_text(json.dumps(self.d, indent=2), encoding="utf-8")
        tmp.replace(self.dir / "job.json")

    def summary(self) -> dict:
        cur = next((s for s in reversed(self.tracker.steps) if s["status"] in ("running", "retrying")), None)
        return {**self.d, "steps": self.tracker.steps, "current": cur["title"] if cur else None,
                "lineCount": len(self.lines), **{k: v for k, v in self.tracker.summary.items() if k != "report"}}

    def append(self, line: str):
        with self.cond:
            self.lines.append(line)
            self.tracker.feed(line)
            self.cond.notify_all()

    def set_status(self, status: str, **extra):
        with self.cond:
            self.d["status"] = status
            self.d.update(extra)
            if status in TERMINAL:
                self.tracker.close(status)
            self.save()
            self.cond.notify_all()


class JobStore:
    def __init__(self, root: Path, parallel: int):
        self.root = root
        self.parallel = max(1, parallel)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.schema = option_schema()
        (root / "jobs").mkdir(parents=True, exist_ok=True)
        settings = root / "settings.json"
        if settings.exists():
            try:
                self.parallel = int(json.loads(settings.read_text()).get("parallel", self.parallel))
            except (ValueError, OSError):
                pass
        for d in sorted((root / "jobs").iterdir()):
            f = d / "job.json"
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                if data.get("status") in ("running", "preparing"):
                    data["status"] = "error"
                    data["error"] = "the dashboard was stopped while this job ran"
                self.jobs[data["id"]] = Job(self, data)
        threading.Thread(target=self._scheduler, daemon=True).start()

    def save_settings(self):
        (self.root / "settings.json").write_text(json.dumps({"parallel": self.parallel}), encoding="utf-8")

    def create(self, command: str, project: str, options: dict, copy: bool) -> Job:
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        job = Job(self, {"id": jid, "command": command, "project": project, "name": Path(project).name, "options": options,
                         "copy": copy, "status": "queued", "created": time.time(), "started": None, "ended": None,
                         "exitCode": None, "workdir": None, "error": None})
        job.save()
        with self.lock:
            self.jobs[jid] = job
            self.wake.notify_all()
        return job

    def running(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.d["status"] in ("running", "preparing", "cancelling")]

    def _scheduler(self):
        while True:
            with self.lock:
                while True:
                    queued = sorted((j for j in self.jobs.values() if j.d["status"] == "queued"), key=lambda j: j.d["created"])
                    if queued and len(self.running()) < self.parallel:
                        job = queued[0]
                        job.d["status"] = "preparing"
                        break
                    self.wake.wait(timeout=2)
            threading.Thread(target=self._run, args=(job,), daemon=True).start()

    def _run(self, job: Job):
        try:
            project = Path(job.d["project"]).expanduser().resolve()
            if not project.is_dir():
                raise RuntimeError(f"not a directory: {project}")
            work = project
            if job.d["copy"]:
                work = job.dir / "workspace" / project.name
                job.append(f"[dashboard] copying {project} -> {work} (the original stays untouched)")
                shutil.rmtree(work, ignore_errors=True)
                shutil.copytree(project, work, ignore=build_output_ignore)
                self._prepare_copy_git(job, work)
            out_file = job.dir / ("scan.md" if job.d["command"] == "scan" else sm.DEP_PLAN_FILE)
            argv = build_argv(job.d["command"], str(work), job.d["options"], self.schema, job.dir / "report", out_file)
            job.d["argv"] = argv[2:]
            job.append("[dashboard] $ " + " ".join(argv[2:]))
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            job.proc = subprocess.Popen(argv, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env, start_new_session=True)
            job.set_status("running", started=time.time(), workdir=str(work))
            with (job.dir / "output.log").open("a", encoding="utf-8") as log:
                for line in job.proc.stdout:
                    line = line.rstrip("\n")
                    log.write(line + "\n")
                    log.flush()
                    job.append(line)
            code = job.proc.wait()
            if job.d["status"] == "cancelling":
                status = "cancelled"
            elif code == 0:
                status = "succeeded"
            elif code == 1 and job.d["command"] == "migrate":
                status = "stopped"      # a stage's build failed; changes are committed, re-run resumes
            else:
                status = "failed"
            job.set_status(status, exitCode=code, ended=time.time())
        except Exception as e:  # noqa: BLE001 - shown to the user
            job.append(f"[dashboard] error: {e}")
            job.set_status("error", error=str(e), ended=time.time())
        finally:
            with self.lock:
                self.wake.notify_all()

    @staticmethod
    def _prepare_copy_git(job: Job, work: Path):
        """The tool needs a clean git repo. A copy of a sub-folder of a bigger repo has none, and a copy of a checkout
        with local edits is dirty: give the copy its own repo / a snapshot commit (only the copy is touched)."""
        ident = ["-c", "user.name=spring-migrate", "-c", "user.email=spring-migrate@localhost"]
        def g(*args):
            return subprocess.run(["git", *ident, *args], cwd=work, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if not (work / ".git").exists():
            g("init", "-q", "-b", "main")
            g("add", "-A")
            g("commit", "-q", "-m", "baseline (spring-migrate dashboard copy)")
            job.append("[dashboard] the copy had no git repository of its own: created one with a baseline commit")
        elif g("status", "--porcelain").stdout.strip():
            g("add", "-A")
            g("commit", "-q", "-m", "snapshot of uncommitted changes (spring-migrate dashboard copy)")
            job.append("[dashboard] the copy had uncommitted changes: committed them as a snapshot (in the copy only)")
        if not g("config", "user.email").stdout.strip():
            g("config", "user.name", "spring-migrate")
            g("config", "user.email", "spring-migrate@localhost")

    def cancel(self, job: Job):
        if job.d["status"] == "queued":
            job.set_status("cancelled", ended=time.time())
        elif job.proc and job.proc.poll() is None:
            job.set_status("cancelling")
            try:
                os.killpg(job.proc.pid, signal.SIGTERM)   # also stops mvn/gradle children
            except (ProcessLookupError, PermissionError):
                job.proc.terminate()

    def delete(self, job: Job):
        if job.d["status"] not in TERMINAL:
            raise ValueError("cancel the job first")
        with self.lock:
            self.jobs.pop(job.id, None)
        shutil.rmtree(job.dir, ignore_errors=True)


def build_output_ignore(directory: str, names: list[str]) -> set[str]:
    """Skip build output when copying a project: `target` only next to a pom.xml, `build` only next to a Gradle build
    (a folder called build elsewhere can be real source, e.g. in a frontend), plus caches."""
    d = Path(directory)
    skip = {n for n in names if n in (".gradle", "node_modules")}
    if "target" in names and (d / "pom.xml").exists():
        skip.add("target")
    if "build" in names and ((d / "build.gradle").exists() or (d / "build.gradle.kts").exists()):
        skip.add("build")
    return skip


# --------------------------------------------------------------------------- #
# Filesystem helpers for the project picker
# --------------------------------------------------------------------------- #
def project_marker(p: Path) -> str | None:
    for n, kind in (("pom.xml", "maven"), ("build.gradle", "gradle"), ("build.gradle.kts", "gradle")):
        if (p / n).is_file():
            return kind
    return None


def browse(path: str) -> dict:
    p = Path(path or "~").expanduser().resolve()
    if not p.is_dir():
        p = p.parent
    entries = []
    try:
        for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if c.is_dir() and not c.name.startswith("."):
                entries.append({"name": c.name, "path": str(c), "project": project_marker(c)})
    except PermissionError:
        pass
    return {"path": str(p), "parent": str(p.parent), "project": project_marker(p), "entries": entries[:500]}


def discover(path: str, depth: int = 3) -> list[dict]:
    root = Path(path).expanduser().resolve()
    found = []

    def walk(d: Path, level: int):
        kind = project_marker(d)
        if kind:
            found.append({"path": str(d), "name": d.name, "kind": kind, "git": (d / ".git").exists(), **project_info(d)})
            return  # modules of a multi-module build belong to it
        if level >= depth:
            return
        try:
            for c in sorted(d.iterdir()):
                if c.is_dir() and not c.name.startswith(".") and c.name not in sm.IGNORE_DIRS:
                    walk(c, level + 1)
        except PermissionError:
            pass
    walk(root, 0)
    return found[:200]


def project_info(d: Path) -> dict:
    info = sm.detect_build(d)
    return {"boot": info["boot_version"], "javaLevel": info["java_version"]}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    store: JobStore = None
    token: str | None = None
    server_version = "spring-migrate-web"

    def log_message(self, fmt, *args):  # quiet
        pass

    # ---- helpers
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _err(self, code: int, msg: str):
        self._json({"error": msg}, code)

    def _authorized(self) -> bool:
        if not self.token:
            return True
        q = parse_qs(urlparse(self.path).query)
        got = self.headers.get("X-Token") or (q.get("token") or [None])[0]
        cookie = self.headers.get("Cookie") or ""
        m = re.search(r"smw_token=([\w-]+)", cookie)
        return secrets.compare_digest(got or (m.group(1) if m else ""), self.token)

    def _same_origin(self) -> bool:
        """Block cross-site POSTs (another web page in your browser talking to this local server)."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _job(self, jid: str) -> Job | None:
        return self.store.jobs.get(jid)

    # ---- routes
    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            extra = {}
            if self.token and self._authorized():
                extra["Set-Cookie"] = f"smw_token={self.token}; HttpOnly; SameSite=Strict; Path=/"
            elif self.token:
                return self._send(401, b"Open the URL with ?token=... printed by spring_migrate_web.py", "text/plain")
            return self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8",
                              {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'", **extra})
        if not self._authorized():
            return self._err(401, "missing or wrong token")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        if parts == ["api", "meta"]:
            return self._json({"version": sm.__version__, "schema": self.store.schema, "parallel": self.store.parallel,
                               "stateDir": str(self.store.root), "cwd": os.getcwd(), "examples": str(HERE / "examples")})
        if parts == ["api", "jobs"]:
            return self._json([j.summary() for j in sorted(self.store.jobs.values(), key=lambda j: j.d["created"], reverse=True)])
        if parts == ["api", "browse"]:
            return self._json(browse(q.get("path", "")))
        if parts == ["api", "discover"]:
            return self._json(discover(q.get("path", "."), int(q.get("depth", 3))))
        if len(parts) >= 3 and parts[:2] == ["api", "jobs"]:
            job = self._job(parts[2])
            if not job:
                return self._err(404, "no such job")
            if len(parts) == 3:
                return self._json({**job.summary(), "lines": job.lines[-int(q.get("tail", 4000)):]})
            if parts[3] == "events":
                return self._events(job, int(q.get("from", 0)))
            if parts[3] in ("report", "plan", "scan"):
                f = {"report": job.dir / "report" / "MIGRATION_REPORT.md", "plan": job.dir / ("report" if job.d["command"] == "migrate" else "") / sm.DEP_PLAN_FILE,
                     "scan": job.dir / "scan.md"}[parts[3]]
                if parts[3] == "plan" and not f.exists():
                    f = job.dir / sm.DEP_PLAN_FILE
                if not f.exists():
                    return self._err(404, "not produced (yet)")
                return self._send(200, f.read_bytes(), "text/plain; charset=utf-8")
            if parts[3] == "files" and len(parts) == 5 and parts[4] in ("scan-before.md", "scan-after.md"):
                f = job.dir / "report" / parts[4]
                return self._send(200, f.read_bytes(), "text/plain; charset=utf-8") if f.exists() else self._err(404, "not produced")
        return self._err(404, "not found")

    def _events(self, job: Job, start: int):
        """Server-sent events: one 'line' event per output line, 'status' when steps/status change."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        pos, last_sig = start, None
        try:
            while True:
                with job.cond:
                    if pos >= len(job.lines) and job.d["status"] not in TERMINAL:
                        job.cond.wait(timeout=15)
                    new = job.lines[pos:]
                    pos += len(new)
                    summ = job.summary()
                chunks = []
                if new:
                    chunks.append("event: lines\ndata: " + json.dumps(new) + "\n\n")
                sig = json.dumps([summ["status"], [(s["key"], s["status"], s["changes"]) for s in summ["steps"]]])
                if sig != last_sig:
                    chunks.append("event: status\ndata: " + json.dumps(summ, default=str) + "\n\n")
                    last_sig = sig
                if not chunks:
                    chunks.append(": keep-alive\n\n")
                self.wfile.write("".join(chunks).encode())
                self.wfile.flush()
                if summ["status"] in TERMINAL and pos >= len(job.lines):
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        if not self._authorized():
            return self._err(401, "missing or wrong token")
        if not self._same_origin():
            return self._err(403, "cross-origin request refused")
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        try:
            body = self._body()
        except ValueError:
            return self._err(400, "invalid JSON")
        if parts == ["api", "jobs"]:
            command = body.get("command", "migrate")
            if command not in COMMANDS:
                return self._err(400, f"command must be one of {COMMANDS}")
            projects = [p.strip() for p in body.get("projects", []) if str(p).strip()]
            if not projects:
                return self._err(400, "add at least one project path")
            bad = [p for p in projects if not Path(p).expanduser().is_dir()]
            if bad:
                return self._err(400, "not a directory: " + ", ".join(bad))
            jobs = [self.store.create(command, str(Path(p).expanduser().resolve()), body.get("options", {}), bool(body.get("copy")))
                    for p in projects]
            return self._json([j.summary() for j in jobs], 201)
        if parts == ["api", "settings"]:
            self.store.parallel = max(1, min(8, int(body.get("parallel", self.store.parallel))))
            self.store.save_settings()
            with self.store.lock:
                self.store.wake.notify_all()
            return self._json({"parallel": self.store.parallel})
        if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
            job = self._job(parts[2])
            if not job:
                return self._err(404, "no such job")
            if parts[3] == "cancel":
                self.store.cancel(job)
                return self._json(job.summary())
            if parts[3] == "update":
                # apply this tool version's fixes to the branch the job produced (in its copy, if it worked on one)
                target = job.d["workdir"] if job.d.get("copy") and job.d.get("workdir") else job.d["project"]
                keep = {k: v for k, v in (job.d.get("options") or {}).items()
                        if k in ("build_args", "java_home", "skip_frontend", "format_goal", "no_format", "offline", "java")}
                new = self.store.create("update", target, keep, False)
                return self._json(new.summary(), 201)
            if parts[3] == "rerun":
                # same project/options; for migrate this resumes from the current Boot version
                src = job.d["workdir"] if job.d["copy"] and job.d.get("workdir") and body.get("continue", True) else job.d["project"]
                new = self.store.create(job.d["command"], src, job.d["options"], job.d["copy"] and not body.get("continue", True))
                return self._json(new.summary(), 201)
        return self._err(404, "not found")

    def do_DELETE(self):
        if not self._authorized():
            return self._err(401, "missing or wrong token")
        if not self._same_origin():
            return self._err(403, "cross-origin request refused")
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
            job = self._job(parts[2])
            if not job:
                return self._err(404, "no such job")
            try:
                self.store.delete(job)
            except ValueError as e:
                return self._err(409, str(e))
            return self._json({"deleted": job.id})
        return self._err(404, "not found")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="spring-migrate-web", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1 - this server runs commands on your machine)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--parallel", type=int, default=1, help="migrations running at the same time (default 1 = one after the other)")
    ap.add_argument("--state-dir", default=str(Path.home() / ".spring-migrate-web"), help="where jobs, logs and reports are kept")
    ap.add_argument("--token", help="access token (generated automatically when --host is not a loopback address)")
    a = ap.parse_args(argv)
    store = JobStore(Path(a.state_dir).expanduser(), a.parallel)
    Handler.store = store
    loopback = a.host in ("127.0.0.1", "localhost", "::1")
    Handler.token = a.token or (None if loopback else secrets.token_urlsafe(24))
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '::') else a.host}:{a.port}/" + (f"?token={Handler.token}" if Handler.token else "")
    print(f"spring-migrate dashboard on {url}\n  jobs/logs/reports: {store.root}\n  parallel migrations: {store.parallel}", flush=True)
    if not loopback:
        print("  WARNING: reachable from the network. Anyone with the token can run commands on this machine.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>spring-migrate</title>
<style>
:root {
  --bg: #f6f7f9; --panel: #ffffff; --panel-2: #f1f3f6; --border: #e2e5ea; --text: #1b1f24; --muted: #5f6b7a;
  --accent: #2f6f4f; --accent-ink: #ffffff; --accent-soft: #e3f1e9;
  --ok: #2f7d4f; --ok-soft: #e2f3e9; --run: #2b62c4; --run-soft: #e4edfc; --warn: #a4660a; --warn-soft: #fbf0dc;
  --bad: #b8322a; --bad-soft: #fbe5e3; --idle: #6b7280; --idle-soft: #eceef1;
  --log-bg: #0f1419; --log-text: #d7dde4; --log-muted: #7d8794; --log-step: #8fc1ff; --log-change: #9ad6a8; --log-err: #ff8e85; --log-warn: #f3c776;
  --shadow: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.08);
  --radius: 10px; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0e1116; --panel: #161b22; --panel-2: #1c222b; --border: #2a313c; --text: #e6e9ee; --muted: #98a2b0;
    --accent: #5fb588; --accent-ink: #0b1510; --accent-soft: #1b2e24;
    --ok: #63c48a; --ok-soft: #173023; --run: #7aa7f5; --run-soft: #182a45; --warn: #e5b45a; --warn-soft: #34290f;
    --bad: #f0857c; --bad-soft: #3a1a18; --idle: #9aa3af; --idle-soft: #232a33;
    --log-bg: #0a0d11; --shadow: none; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0e1116; --panel: #161b22; --panel-2: #1c222b; --border: #2a313c; --text: #e6e9ee; --muted: #98a2b0;
  --accent: #5fb588; --accent-ink: #0b1510; --accent-soft: #1b2e24;
  --ok: #63c48a; --ok-soft: #173023; --run: #7aa7f5; --run-soft: #182a45; --warn: #e5b45a; --warn-soft: #34290f;
  --bad: #f0857c; --bad-soft: #3a1a18; --idle: #9aa3af; --idle-soft: #232a33;
  --log-bg: #0a0d11; --shadow: none; color-scheme: dark;
}
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
button, input, select, textarea { font: inherit; color: inherit; }
a { color: var(--accent); }
.app { max-width: 1440px; margin: 0 auto; padding: 16px; }
header.top { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 16px; }
.brand { display: flex; align-items: center; gap: 10px; margin-right: auto; }
.brand .mark { width: 30px; height: 30px; border-radius: 8px; background: var(--accent); display: grid; place-items: center; color: var(--accent-ink); font-weight: 700; }
.brand h1 { font-size: 17px; margin: 0; }
.brand .ver { color: var(--muted); font-size: 12px; }
.btn { border: 1px solid var(--border); background: var(--panel); border-radius: 8px; padding: 7px 12px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px; }
.btn:hover { background: var(--panel-2); }
.btn:disabled { opacity: .5; cursor: default; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-ink); font-weight: 600; }
.btn.primary:hover { filter: brightness(1.05); }
.btn.danger { color: var(--bad); }
.btn.small { padding: 4px 9px; font-size: 13px; }
.control { display: inline-flex; align-items: center; gap: 6px; color: var(--muted); font-size: 13px; }
.control select { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 6px 8px; }
.stats { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 16px; }
.stat { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px 14px; box-shadow: var(--shadow); }
.stat .n { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
.stat .l { color: var(--muted); font-size: 12px; }
.grid { display: grid; grid-template-columns: 360px minmax(0, 1fr); gap: 16px; align-items: start; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); }
.panel > .hd { padding: 10px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 8px; }
.panel > .hd h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); margin: 0; margin-right: auto; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; background: transparent; padding: 4px 10px; cursor: pointer; font-size: 12px; color: var(--muted); }
.seg button.on { background: var(--panel-2); color: var(--text); font-weight: 600; }
.jobs { list-style: none; margin: 0; padding: 6px; max-height: calc(100vh - 230px); overflow: auto; }
.job { padding: 10px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; }
.job:hover { background: var(--panel-2); }
.job.sel { border-color: var(--accent); background: var(--accent-soft); }
.job .r1 { display: flex; align-items: center; gap: 8px; }
.job .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-right: auto; }
.job .r2 { color: var(--muted); font-size: 12px; margin-top: 3px; display: flex; gap: 8px; flex-wrap: wrap; }
.bar { height: 4px; background: var(--idle-soft); border-radius: 4px; margin-top: 8px; overflow: hidden; }
.bar > i { display: block; height: 100%; background: var(--run); transition: width .4s; }
.pill { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; white-space: nowrap; }
.s-queued, .s-cancelled { background: var(--idle-soft); color: var(--idle); }
.s-preparing, .s-running, .s-cancelling { background: var(--run-soft); color: var(--run); }
.s-succeeded { background: var(--ok-soft); color: var(--ok); }
.s-stopped { background: var(--warn-soft); color: var(--warn); }
.s-failed, .s-error { background: var(--bad-soft); color: var(--bad); }
.empty { padding: 32px 18px; text-align: center; color: var(--muted); }
.detail .hd2 { padding: 14px 16px; border-bottom: 1px solid var(--border); display: flex; gap: 12px; align-items: flex-start; flex-wrap: wrap; }
.detail .hd2 .t { margin-right: auto; min-width: 0; }
.detail .hd2 h2 { margin: 0; font-size: 18px; display: flex; align-items: center; gap: 10px; }
.detail .path { color: var(--muted); font-family: var(--mono); font-size: 12px; word-break: break-all; }
.detail .meta { color: var(--muted); font-size: 12px; margin-top: 4px; display: flex; gap: 12px; flex-wrap: wrap; }
.actions { display: flex; gap: 6px; flex-wrap: wrap; }
.body2 { display: grid; grid-template-columns: 320px minmax(0, 1fr); }
.steps { list-style: none; margin: 0; padding: 12px 14px; border-right: 1px solid var(--border); }
.step { display: grid; grid-template-columns: 22px 1fr; gap: 8px; padding: 6px 0; position: relative; }
.step:not(:last-child)::after { content: ""; position: absolute; left: 10px; top: 28px; bottom: -6px; width: 2px; background: var(--border); }
.dot { width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; font-size: 12px; font-weight: 700; z-index: 1; }
.d-ok { background: var(--ok-soft); color: var(--ok); }
.d-running, .d-retrying { background: var(--run-soft); color: var(--run); }
.d-failed { background: var(--bad-soft); color: var(--bad); }
.d-cancelled, .d-pending { background: var(--idle-soft); color: var(--idle); }
.d-running::before { content: ""; width: 10px; height: 10px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .d-running::before { animation: none; } .bar > i { transition: none; } }
.step .st { font-weight: 600; font-size: 13px; }
.step .sm { color: var(--muted); font-size: 12px; }
.step details { font-size: 12px; color: var(--muted); }
.step details summary { cursor: pointer; }
.tabs { display: flex; gap: 2px; padding: 8px 12px 0; border-bottom: 1px solid var(--border); flex-wrap: wrap; }
.tabs button { border: 0; background: none; padding: 8px 12px; cursor: pointer; color: var(--muted); border-bottom: 2px solid transparent; }
.tabs button.on { color: var(--text); border-bottom-color: var(--accent); font-weight: 600; }
.tabpane { padding: 12px; min-width: 0; }
.logtools { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.logtools input[type=search] { flex: 1; min-width: 140px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; }
.log { background: var(--log-bg); color: var(--log-text); font: 12px/1.5 var(--mono); border-radius: 8px; padding: 10px 12px; height: calc(100vh - 390px); min-height: 320px; overflow: auto; white-space: pre-wrap; word-break: break-word; }
.log .l-step { color: var(--log-step); font-weight: 700; }
.log .l-change { color: var(--log-change); }
.log .l-err { color: var(--log-err); }
.log .l-warn { color: var(--log-warn); }
.log .l-muted { color: var(--log-muted); }
.md { max-width: 980px; overflow-x: auto; }
.md h1 { font-size: 20px; } .md h2 { font-size: 16px; margin-top: 22px; } .md h3 { font-size: 14px; }
.md table { border-collapse: collapse; font-size: 13px; margin: 8px 0; }
.md th, .md td { border: 1px solid var(--border); padding: 4px 8px; text-align: left; vertical-align: top; }
.md th { background: var(--panel-2); }
.md code { font-family: var(--mono); font-size: 12px; background: var(--panel-2); padding: 1px 4px; border-radius: 4px; }
.md blockquote { margin: 8px 0; padding: 4px 12px; border-left: 3px solid var(--warn); background: var(--warn-soft); }
pre.cmd { background: var(--panel-2); border: 1px solid var(--border); padding: 10px; border-radius: 8px; font: 12px/1.5 var(--mono); white-space: pre-wrap; word-break: break-all; }
dialog { border: 1px solid var(--border); border-radius: 12px; padding: 0; background: var(--panel); color: var(--text); width: min(920px, calc(100vw - 24px)); max-height: calc(100vh - 32px); }
dialog::backdrop { background: rgba(10, 14, 20, .45); }
dialog form { display: flex; flex-direction: column; max-height: calc(100vh - 34px); }
dialog .dh { padding: 14px 18px; border-bottom: 1px solid var(--border); display: flex; align-items: center; }
dialog .dh h2 { margin: 0; font-size: 16px; margin-right: auto; }
dialog .db { padding: 14px 18px; overflow: auto; }
dialog .df { padding: 12px 18px; border-top: 1px solid var(--border); display: flex; gap: 8px; justify-content: flex-end; align-items: center; }
fieldset { border: 1px solid var(--border); border-radius: 10px; margin: 0 0 12px; padding: 10px 12px; }
legend { font-weight: 600; font-size: 13px; padding: 0 4px; }
textarea, .field input[type=text], .field input[type=number], .field select { width: 100%; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 7px 9px; }
textarea { font-family: var(--mono); font-size: 12px; min-height: 84px; resize: vertical; }
.fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 10px 14px; }
.field label { display: block; font-size: 12px; font-weight: 600; margin-bottom: 3px; }
.field .h { color: var(--muted); font-size: 11.5px; margin-top: 2px; }
.check { display: flex; gap: 8px; align-items: flex-start; }
.check input { margin-top: 3px; }
.check label { font-weight: 600; font-size: 12.5px; }
.radio-row { display: flex; gap: 8px; flex-wrap: wrap; }
.radio-row label { border: 1px solid var(--border); border-radius: 8px; padding: 8px 12px; cursor: pointer; display: flex; gap: 6px; align-items: center; }
.radio-row label:has(input:checked) { border-color: var(--accent); background: var(--accent-soft); }
.browser { border: 1px solid var(--border); border-radius: 8px; margin-top: 8px; }
.browser .bh { display: flex; gap: 6px; align-items: center; padding: 6px 8px; border-bottom: 1px solid var(--border); }
.browser .bh code { font-family: var(--mono); font-size: 12px; margin-right: auto; word-break: break-all; }
.browser ul { list-style: none; margin: 0; padding: 4px; max-height: 220px; overflow: auto; }
.browser li { display: flex; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 6px; }
.browser li:hover { background: var(--panel-2); }
.browser li .nm { cursor: pointer; margin-right: auto; }
.tag { font-size: 11px; padding: 1px 6px; border-radius: 4px; background: var(--accent-soft); color: var(--accent); }
.note { color: var(--muted); font-size: 12px; }
.toast { position: fixed; bottom: 16px; right: 16px; background: var(--text); color: var(--bg); padding: 10px 14px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity .2s; pointer-events: none; max-width: 420px; }
.toast.on { opacity: 1; }
@media (max-width: 1100px) { .grid { grid-template-columns: 1fr; } .jobs { max-height: 320px; } }
@media (max-width: 760px) { .body2 { grid-template-columns: 1fr; } .steps { border-right: 0; border-bottom: 1px solid var(--border); } .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="brand"><div class="mark" aria-hidden="true">sm</div><div><h1>spring-migrate</h1><div class="ver" id="ver"></div></div></div>
    <label class="control">Run at the same time
      <select id="parallel" aria-label="Migrations running at the same time">
        <option value="1">1 (one after the other)</option><option value="2">2</option><option value="3">3</option><option value="4">4</option>
      </select>
    </label>
    <button class="btn" id="themeBtn" type="button" title="Toggle theme">Theme</button>
    <button class="btn primary" id="newBtn" type="button">+ New migration</button>
  </header>

  <section class="stats" aria-label="Summary">
    <div class="stat"><div class="n" id="cRunning">0</div><div class="l">Running</div></div>
    <div class="stat"><div class="n" id="cQueued">0</div><div class="l">Queued</div></div>
    <div class="stat"><div class="n" id="cOk">0</div><div class="l">Succeeded</div></div>
    <div class="stat"><div class="n" id="cStopped">0</div><div class="l">Stopped at a stage</div></div>
    <div class="stat"><div class="n" id="cFailed">0</div><div class="l">Failed / cancelled</div></div>
  </section>

  <div class="grid">
    <section class="panel">
      <div class="hd"><h2>Jobs</h2>
        <div class="seg" id="filter"><button class="on" data-f="all">All</button><button data-f="active">Active</button><button data-f="done">Done</button></div>
      </div>
      <ul class="jobs" id="jobs"></ul>
    </section>
    <section class="panel detail" id="detail">
      <div class="empty" id="noSel">
        <p><strong>No job selected.</strong></p>
        <p>Start with <em>+ New migration</em>: pick one or more projects, choose the options, and watch every stage live here.</p>
      </div>
    </section>
  </div>
</div>

<dialog id="dlg">
  <form method="dialog" id="newForm">
    <div class="dh"><h2>New migration</h2><button class="btn small" value="cancel" formnovalidate>Close</button></div>
    <div class="db">
      <fieldset>
        <legend>Projects</legend>
        <div class="field"><label for="projects">One project directory per line (each becomes its own job)</label>
          <textarea id="projects" placeholder="/home/me/src/orders-service&#10;/home/me/src/billing"></textarea></div>
        <div style="display:flex; gap:8px; margin-top:8px; flex-wrap:wrap">
          <button class="btn small" type="button" id="browseBtn">Browse folders…</button>
          <button class="btn small" type="button" id="discoverBtn">Find projects in a folder…</button>
          <span class="note" id="projCount"></span>
        </div>
        <div class="browser" id="browser" hidden>
          <div class="bh"><button class="btn small" type="button" id="upBtn">Up</button><code id="bPath"></code>
            <button class="btn small" type="button" id="addHere">Add this folder</button>
            <button class="btn small" type="button" id="discoverHere">Find projects here</button></div>
          <ul id="bList"></ul>
        </div>
      </fieldset>
      <fieldset>
        <legend>What to run</legend>
        <div class="radio-row" id="cmdRow">
          <label><input type="radio" name="cmd" value="migrate" checked> Migrate</label>
          <label><input type="radio" name="cmd" value="scan"> Scan only (read-only)</label>
          <label><input type="radio" name="cmd" value="deps"> Company dependency plan</label>
          <label><input type="radio" name="cmd" value="update"> Update a migrated branch</label>
        </div>
        <div class="check" style="margin-top:10px"><input type="checkbox" id="copy" checked><div><label for="copy">Work on a copy</label>
          <div class="note">Copies the project into the dashboard's state folder first, so your checkout is never touched (the tool still creates a migration branch in the copy).</div></div></div>
      </fieldset>
      <div id="optForm"></div>
    </div>
    <div class="df"><span class="note" id="formErr" role="alert" style="margin-right:auto; color: var(--bad)"></span>
      <button class="btn" value="cancel" formnovalidate>Cancel</button>
      <button class="btn primary" id="submitBtn" value="default">Queue</button></div>
  </form>
</dialog>
<div class="toast" id="toast" role="status"></div>

<script>
(() => {
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem("smw." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("smw." + k, JSON.stringify(v)); } catch {} },
};
let META = null, JOBS = [], SEL = store.get("sel", null), FILTER = "all", ES = null, LOGLINES = [], TAB = "log";

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const ct = r.headers.get("Content-Type") || "";
  const body = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body && body.error ? body.error : (typeof body === "string" ? body : r.statusText));
  return body;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("on"), 3500); }
const fmtDur = s => { if (s == null || s < 0) return ""; s = Math.round(s); if (s < 60) return s + "s"; const m = Math.floor(s / 60); return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`; };
const ago = t => { if (!t) return ""; const s = Date.now() / 1000 - t; if (s < 60) return "just now"; if (s < 3600) return Math.floor(s / 60) + " min ago"; if (s < 86400) return Math.floor(s / 3600) + " h ago"; return new Date(t * 1000).toLocaleDateString(); };
const STATUS_LABEL = { queued: "Queued", preparing: "Starting", running: "Running", cancelling: "Cancelling", succeeded: "Succeeded", stopped: "Stopped at a stage", failed: "Failed", cancelled: "Cancelled", error: "Error" };
const ACTIVE = ["queued", "preparing", "running", "cancelling"];
const EXPECTED = { migrate: ["preflight", "baseline", "deps", "1-", "2-", "3-", "4-"], scan: [], deps: ["deps"] };

// ---------------- theme
const applyTheme = t => { if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme"); };
applyTheme(store.get("theme", null));
$("#themeBtn").onclick = () => {
  const cur = document.documentElement.getAttribute("data-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark"; applyTheme(next); store.set("theme", next);
};

// ---------------- jobs list
function progress(j) {
  if (j.status === "succeeded") return 100;
  if (!j.steps || !j.steps.length) return ACTIVE.includes(j.status) ? 3 : 0;
  const target = (j.options && j.options.target) || "4.1";
  const total = j.command === "migrate" ? 4 + ({"3.5": 1, "4.0": 2, "4.1": 3}[target] || 3) : 2;
  const done = j.steps.filter(s => s.status === "ok").length;
  return Math.min(98, Math.round(100 * done / total));
}
function renderStats() {
  const c = s => JOBS.filter(j => s.includes(j.status)).length;
  $("#cRunning").textContent = c(["running", "preparing", "cancelling"]);
  $("#cQueued").textContent = c(["queued"]);
  $("#cOk").textContent = c(["succeeded"]);
  $("#cStopped").textContent = c(["stopped"]);
  $("#cFailed").textContent = c(["failed", "error", "cancelled"]);
}
function renderJobs() {
  renderStats();
  const list = JOBS.filter(j => FILTER === "all" || (FILTER === "active" ? ACTIVE.includes(j.status) : !ACTIVE.includes(j.status)));
  const ul = $("#jobs");
  if (!list.length) { ul.innerHTML = `<li class="empty">${JOBS.length ? "Nothing in this view." : "No jobs yet."}</li>`; return; }
  ul.innerHTML = list.map(j => `
    <li class="job ${j.id === SEL ? "sel" : ""}" data-id="${esc(j.id)}" tabindex="0">
      <div class="r1"><span class="name" title="${esc(j.project)}">${esc(j.name)}</span><span class="pill s-${esc(j.status)}">${esc(STATUS_LABEL[j.status] || j.status)}</span></div>
      <div class="r2"><span>${esc(j.command)}${j.command === "migrate" ? " → " + esc((j.options && j.options.target) || "4.1") : ""}</span>
        <span>${esc(j.current || (j.stopped_at ? "stopped at " + j.stopped_at : ""))}</span>
        <span>${j.started ? fmtDur((j.ended || Date.now() / 1000) - j.started) : ago(j.created)}</span></div>
      ${ACTIVE.includes(j.status) || j.status === "succeeded" ? `<div class="bar"><i style="width:${progress(j)}%; ${j.status === "succeeded" ? "background:var(--ok)" : ""}"></i></div>` : ""}
    </li>`).join("");
  ul.querySelectorAll(".job").forEach(li => {
    li.onclick = () => select(li.dataset.id);
    li.onkeydown = e => { if (e.key === "Enter") select(li.dataset.id); };
  });
}
async function refreshJobs() {
  try { JOBS = await api("/api/jobs"); renderJobs(); if (SEL && !JOBS.find(j => j.id === SEL)) { SEL = null; renderDetailEmpty(); } }
  catch (e) { /* server restarting */ }
}
$("#filter").onclick = e => { const b = e.target.closest("button"); if (!b) return; FILTER = b.dataset.f; $("#filter").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); renderJobs(); };

// ---------------- detail
function renderDetailEmpty() { $("#detail").innerHTML = `<div class="empty"><p><strong>No job selected.</strong></p></div>`; }
function select(id) {
  SEL = id; store.set("sel", id); renderJobs();
  const j = JOBS.find(x => x.id === id); if (!j) return;
  $("#detail").innerHTML = `
    <div class="hd2"><div class="t"><h2><span id="dName"></span><span class="pill" id="dStatus"></span></h2>
      <div class="path" id="dPath"></div><div class="meta" id="dMeta"></div></div>
      <div class="actions">
        <button class="btn small danger" id="aCancel" type="button">Cancel</button>
        <button class="btn small" id="aRerun" type="button" title="Run again with the same options. A migration resumes from the Boot version it reached.">Re-run / resume</button>
        <button class="btn small" id="aUpdate" type="button" title="Apply the fixes of the current spring-migrate version to this branch (no full re-run)">Apply tool updates</button>
        <button class="btn small" id="aDelete" type="button">Delete</button>
      </div></div>
    <div class="body2">
      <ol class="steps" id="steps" aria-label="Steps"></ol>
      <div style="min-width:0">
        <div class="tabs" role="tablist">
          <button data-t="log">Live log</button><button data-t="report">Report</button><button data-t="plan">Dependency plan</button>
          <button data-t="scan">Scan</button><button data-t="cmd">Command</button>
        </div>
        <div class="tabpane" id="pane"></div>
      </div>
    </div>`;
  $("#detail").querySelectorAll(".tabs button").forEach(b => b.onclick = () => showTab(b.dataset.t));
  $("#aCancel").onclick = async () => { await api(`/api/jobs/${id}/cancel`, { method: "POST", body: "{}" }); toast("Cancelling…"); refreshJobs(); };
  $("#aRerun").onclick = async () => { const n = await api(`/api/jobs/${id}/rerun`, { method: "POST", body: JSON.stringify({ continue: true }) }); toast("Queued again"); await refreshJobs(); select(n.id); };
  $("#aUpdate").onclick = async () => { const n = await api(`/api/jobs/${id}/update`, { method: "POST", body: "{}" }); toast("Update queued"); await refreshJobs(); select(n.id); };
  $("#aDelete").onclick = async () => { if (!confirm("Delete this job, its log, report and workspace copy?")) return; try { await api(`/api/jobs/${id}`, { method: "DELETE" }); SEL = null; await refreshJobs(); $("#detail").innerHTML = '<div class="empty"><p>Deleted.</p></div>'; } catch (e) { toast(e.message); } };
  LOGLINES = [];
  updateHeader(j);
  showTab(j.command === "scan" ? "scan" : (TAB === "scan" && j.command !== "scan" ? "log" : TAB));
  stream(id);
}
function updateHeader(j) {
  if (!$("#dName")) return;
  $("#dName").textContent = j.name;
  const st = $("#dStatus"); st.className = "pill s-" + j.status; st.textContent = STATUS_LABEL[j.status] || j.status;
  $("#dPath").textContent = j.project + (j.copy && j.workdir ? "  →  copy: " + j.workdir : "");
  const o = j.options || {};
  const bits = [j.command, j.command === "migrate" ? "target Boot " + (o.target || "4.1") + ", Java " + (o.java || 17) : "",
    j.started ? "ran " + fmtDur((j.ended || Date.now() / 1000) - j.started) : "queued " + ago(j.created),
    j.exitCode != null ? "exit code " + j.exitCode : "", j.error || ""];
  $("#dMeta").innerHTML = bits.filter(Boolean).map(b => `<span>${esc(b)}</span>`).join("");
  $("#aCancel").disabled = !ACTIVE.includes(j.status);
  $("#aRerun").disabled = ACTIVE.includes(j.status);
  $("#aUpdate").disabled = ACTIVE.includes(j.status) || j.command === "scan" || j.command === "deps";
  $("#aDelete").disabled = ACTIVE.includes(j.status);
  renderSteps(j);
}
function renderSteps(j) {
  const ol = $("#steps"); if (!ol) return;
  const steps = (j.steps || []).slice();
  if (!steps.length) {
    const msg = j.status === "queued" ? "Waiting for a free slot…" : ACTIVE.includes(j.status) ? "Starting…"
      : j.command === "scan" ? (j.status === "succeeded" ? "Scan finished - see the Scan tab." : "The scan did not finish - see the log.")
      : "No steps recorded - see the log.";
    ol.innerHTML = `<li class="note">${msg}</li>`; return;
  }
  const icon = { ok: "✓", failed: "✕", cancelled: "–", retrying: "↻", running: "", pending: "" };
  ol.innerHTML = steps.map(s => `
    <li class="step"><div class="dot d-${esc(s.status)}" aria-label="${esc(s.status)}">${icon[s.status] ?? ""}</div>
      <div><div class="st">${esc(s.title || s.key)}</div>
        <div class="sm">${esc(s.key)}${s.started ? " · " + fmtDur((s.ended || Date.now() / 1000) - s.started) : ""}${s.changes ? " · " + s.changes + " tool change" + (s.changes > 1 ? "s" : "") : ""}</div>
        ${s.notes && s.notes.length ? `<details><summary>${s.notes.length} note${s.notes.length > 1 ? "s" : ""}</summary>${s.notes.map(n => `<div>${esc(n)}</div>`).join("")}</details>` : ""}
      </div></li>`).join("");
}
function lineClass(l) {
  if (/^\[stage |^\[(preflight|baseline|deps|done|check)\]/.test(l)) return /FAILED|failed|BLOCKERS|stopped/.test(l) ? "l-err l-step" : "l-step";
  if (/^\s+(legacy|fixer|bump|deps|openjpa|props-migrator):/.test(l)) return "l-change";
  if (/\[ERROR\]|^error:|FAILED|Exception|cannot find symbol/.test(l)) return "l-err";
  if (/TODO|WARN|retrying/.test(l)) return "l-warn";
  if (/^\[dashboard\]|^\[git\]|^\s*\(full log/.test(l)) return "l-muted";
  return "";
}
const LOG_FILTERS = { all: () => true, steps: l => /^\[|^\s+(legacy|fixer|bump|deps|openjpa|props-migrator):|TODO/.test(l), errors: l => /ERROR|FAILED|error:|Exception|TODO|cannot find symbol/.test(l) };
function showTab(t) {
  TAB = t; store.set("tab", t);
  $("#detail").querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.t === t));
  const pane = $("#pane"); const j = JOBS.find(x => x.id === SEL);
  if (t === "log") {
    const f = store.get("logFilter", "all"), auto = store.get("autoscroll", true);
    pane.innerHTML = `<div class="logtools">
        <div class="seg" id="lf"><button data-f="all">Everything</button><button data-f="steps">Steps &amp; changes</button><button data-f="errors">Errors &amp; TODOs</button></div>
        <input type="search" id="lq" placeholder="Search the log">
        <label class="control"><input type="checkbox" id="auto" ${auto ? "checked" : ""}> follow</label></div>
      <div class="log" id="log" tabindex="0" aria-label="Log output"></div>`;
    $("#lf").querySelectorAll("button").forEach(b => { b.classList.toggle("on", b.dataset.f === f); b.onclick = () => { store.set("logFilter", b.dataset.f); showTab("log"); }; });
    $("#lq").oninput = () => paintLog(true);
    $("#auto").onchange = e => store.set("autoscroll", e.target.checked);
    paintLog(true);
  } else if (t === "report" || t === "plan" || t === "scan") {
    pane.innerHTML = `<div class="note">Loading…</div>`;
    api(`/api/jobs/${SEL}/${t}`).then(txt => {
      if (t === "plan") pane.innerHTML = planHtml(JSON.parse(txt));
      else pane.innerHTML = `<div class="md">${md(txt)}</div>`;
    }).catch(() => { pane.innerHTML = `<div class="empty">${t === "report" ? "The report is written when the migration ends." : t === "plan" ? "No dependency plan (yet). Migrations and dependency-plan jobs on Maven projects create one." : "Run a scan job to see this."}</div>`; });
  } else if (t === "cmd") {
    pane.innerHTML = j ? `<p class="note">What the dashboard ran (copy it to run the same in a terminal):</p><pre class="cmd">${esc("python3 " + (j.argv || []).map(a => /[\s"'$]/.test(a) ? "'" + a.replace(/'/g, "'\\''") + "'" : a).join(" "))}</pre>
      <p class="note">Options:</p><pre class="cmd">${esc(JSON.stringify(j.options, null, 2))}</pre>` : "";
  }
}
function paintLog(full) {
  const el = $("#log"); if (!el) return;
  const f = LOG_FILTERS[store.get("logFilter", "all")]; const q = ($("#lq") && $("#lq").value || "").toLowerCase();
  const lines = LOGLINES.filter(l => f(l) && (!q || l.toLowerCase().includes(q)));
  const shown = lines.slice(-6000);
  el.innerHTML = (lines.length > shown.length ? `<div class="l-muted">… ${lines.length - shown.length} earlier lines hidden</div>` : "") +
    shown.map(l => `<div class="${lineClass(l)}">${esc(l) || "&nbsp;"}</div>`).join("");
  if (store.get("autoscroll", true)) el.scrollTop = el.scrollHeight;
}
function appendLog(lines) {
  LOGLINES.push(...lines);
  const el = $("#log"); if (!el || TAB !== "log") return;
  const f = LOG_FILTERS[store.get("logFilter", "all")]; const q = ($("#lq") && $("#lq").value || "").toLowerCase();
  const frag = document.createDocumentFragment();
  for (const l of lines) { if (!f(l) || (q && !l.toLowerCase().includes(q))) continue; const d = document.createElement("div"); d.className = lineClass(l); d.textContent = l || " "; frag.appendChild(d); }
  el.appendChild(frag);
  while (el.childElementCount > 8000) el.firstElementChild.remove();
  if (store.get("autoscroll", true)) el.scrollTop = el.scrollHeight;
}
function stream(id) {
  if (ES) ES.close();
  ES = new EventSource(`/api/jobs/${id}/events?from=0`);
  ES.addEventListener("lines", e => { if (SEL === id) appendLog(JSON.parse(e.data)); });
  ES.addEventListener("status", e => {
    const j = JSON.parse(e.data); const i = JOBS.findIndex(x => x.id === j.id);
    if (i >= 0) JOBS[i] = { ...JOBS[i], ...j }; else JOBS.unshift(j);
    renderJobs(); if (SEL === id) updateHeader(j);
  });
  ES.addEventListener("end", () => { ES.close(); ES = null; if (SEL === id && TAB !== "log") showTab(TAB); });
}

// ---------------- tiny markdown renderer (reports are generated by spring_migrate.py)
function inline(s) {
  return esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}
function md(src) {
  const out = []; const lines = src.split("\n"); let i = 0, list = false;
  const closeList = () => { if (list) { out.push("</ul>"); list = false; } };
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*\|/.test(l) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      closeList();
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      let h = `<table><thead><tr>${cells(l).map(c => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>`;
      i += 2;
      while (i < lines.length && /^\s*\|/.test(lines[i])) { h += `<tr>${cells(lines[i]).map(c => `<td>${inline(c)}</td>`).join("")}</tr>`; i++; }
      out.push(h + "</tbody></table>"); continue;
    }
    let m;
    if ((m = l.match(/^(#{1,4})\s+(.*)$/))) { closeList(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); }
    else if ((m = l.match(/^\s*[-*]\s+(.*)$/))) { if (!list) { out.push("<ul>"); list = true; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = l.match(/^>\s?(.*)$/))) { closeList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); }
    else if (l.trim() === "") { closeList(); }
    else { closeList(); out.push(`<p>${inline(l)}</p>`); }
    i++;
  }
  closeList(); return out.join("\n");
}
function planHtml(plan) {
  const lines = plan.lines || ["3.5", "4.0", "4.1"];
  const label = l => l === "2.7-hibernate" ? "Boot 2.7 + Hibernate" : "Boot " + l;
  if (!plan.dependencies || !plan.dependencies.length) return `<div class="empty">No company parent, BOM or explicitly versioned library outside Spring was found.</div>`;
  return `<div class="md"><p class="note">Chosen versions per stage. To change them, edit <code>dependency-plan.json</code> from the report folder and start a migration with the <em>Dependency plan</em> option.</p>
    <table><thead><tr><th>Dependency</th><th>Kind</th><th>Current</th><th>Built against</th>${lines.map(l => `<th>${esc(label(l))}</th>`).join("")}<th>Status</th></tr></thead><tbody>
    ${plan.dependencies.map(d => `<tr><td><code>${esc(d.coordinate)}</code>${d.internal ? ' <span class="tag">internal</span>' : ""}</td><td>${esc(d.kind)}</td><td>${esc(d.current)}</td>
      <td>${esc((d.builtAgainst || {})[d.current] || "")}</td>${lines.map(l => `<td>${esc((d.upgrades || {})[l] || "–")}</td>`).join("")}<td>${esc(d.status)}</td></tr>`).join("")}
    </tbody></table></div>`;
}

// ---------------- new-migration dialog
const GROUP_ORDER = ["Target", "Run", "Migration steps", "Company dependencies", "Build", "Advanced"];
function renderOptions(cmd) {
  const saved = store.get("opts." + cmd, {});
  const opts = META.schema[cmd] || [];
  const groups = {};
  opts.forEach(o => (groups[o.group] = groups[o.group] || []).push(o));
  const field = o => {
    const id = "o_" + o.dest, val = saved[o.dest] ?? o.default, help = `<div class="h">${esc(o.help)}</div>`;
    const name = o.flag.replace(/^--/, "");
    if (o.kind === "flag") return `<div class="check"><input type="checkbox" id="${id}" data-dest="${o.dest}" ${val ? "checked" : ""}><div><label for="${id}">${esc(name)}</label>${help}</div></div>`;
    if (o.kind === "choice") return `<div class="field"><label for="${id}">${esc(name)}</label><select id="${id}" data-dest="${o.dest}">${o.choices.map(c => `<option ${String(val) === c ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>${help}</div>`;
    if (o.kind === "list") return `<div class="field"><label for="${id}">${esc(name)}</label><input type="text" id="${id}" data-dest="${o.dest}" data-list="1" value="${esc(Array.isArray(val) ? val.join(", ") : (val || ""))}" placeholder="comma separated">${help}</div>`;
    return `<div class="field"><label for="${id}">${esc(name)}</label><input type="${o.kind === "int" ? "number" : "text"}" id="${id}" data-dest="${o.dest}" value="${esc(val ?? "")}" ${o.kind === "int" ? 'min="0"' : ""}>${help}</div>`;
  };
  $("#optForm").innerHTML = GROUP_ORDER.filter(g => groups[g]).map(g => g === "Advanced"
    ? `<details><summary style="cursor:pointer; font-weight:600; margin-bottom:8px">Advanced</summary><fieldset><div class="fields">${groups[g].map(field).join("")}</div></fieldset></details>`
    : `<fieldset><legend>${esc(g)}</legend><div class="fields">${groups[g].map(field).join("")}</div></fieldset>`).join("");
  $("#submitBtn").textContent = {migrate: "Queue migration", scan: "Queue scan", deps: "Queue dependency plan", update: "Queue update"}[cmd];
}
function collectOptions() {
  const o = {};
  $("#optForm").querySelectorAll("[data-dest]").forEach(el => {
    const d = el.dataset.dest;
    if (el.type === "checkbox") o[d] = el.checked;
    else if (el.dataset.list) o[d] = el.value.split(",").map(s => s.trim()).filter(Boolean);
    else if (el.type === "number") o[d] = el.value === "" ? null : Number(el.value);
    else o[d] = el.value.trim();
  });
  return o;
}
const cmdValue = () => $("#cmdRow").querySelector("input:checked").value;
const projectList = () => $("#projects").value.split("\n").map(s => s.trim()).filter(Boolean);
function updateCount() { const n = projectList().length; $("#projCount").textContent = n ? `${n} project${n > 1 ? "s" : ""} → ${n} job${n > 1 ? "s" : ""}` : ""; }
function addProjects(paths) {
  const cur = projectList(); paths.forEach(p => { if (!cur.includes(p)) cur.push(p); });
  $("#projects").value = cur.join("\n"); updateCount(); store.set("projects", cur);
}
$("#projects").oninput = () => { updateCount(); store.set("projects", projectList()); };
$("#cmdRow").onchange = () => renderOptions(cmdValue());
$("#newBtn").onclick = () => {
  $("#projects").value = store.get("projects", []).join("\n"); updateCount();
  $("#copy").checked = store.get("copy", true);
  const c = store.get("cmd", "migrate"); $("#cmdRow").querySelector(`input[value="${c}"]`).checked = true;
  renderOptions(c); $("#formErr").textContent = ""; $("#browser").hidden = true; $("#dlg").showModal();
};
async function browseTo(path) {
  try {
    const b = await api("/api/browse?path=" + encodeURIComponent(path || ""));
    $("#browser").hidden = false; $("#bPath").textContent = b.path; $("#bPath").dataset.path = b.path; $("#upBtn").dataset.path = b.parent;
    $("#bList").innerHTML = b.entries.map(e => `<li><span class="nm" data-path="${esc(e.path)}">📁 ${esc(e.name)}</span>${e.project ? `<span class="tag">${esc(e.project)}</span><button class="btn small" type="button" data-add="${esc(e.path)}">Add</button>` : ""}</li>`).join("") || `<li class="note">No sub-folders.</li>`;
    $("#bList").querySelectorAll(".nm").forEach(n => n.onclick = () => browseTo(n.dataset.path));
    $("#bList").querySelectorAll("[data-add]").forEach(n => n.onclick = () => { addProjects([n.dataset.add]); toast("Added " + n.dataset.add); });
    store.set("browse", b.path);
  } catch (e) { toast(e.message); }
}
async function discoverIn(path) {
  try {
    const found = await api("/api/discover?path=" + encodeURIComponent(path));
    if (!found.length) { toast("No Maven/Gradle projects found under " + path); return; }
    $("#browser").hidden = false; $("#bPath").textContent = `${found.length} project(s) under ${path}`;
    $("#bList").innerHTML = `<li><button class="btn small" type="button" id="addAll">Add all ${found.length}</button></li>` + found.map(p => `<li><span class="nm" title="${esc(p.path)}">${esc(p.name)}</span>
      <span class="note">${esc(p.kind)}${p.boot ? " · Boot " + esc(p.boot) : ""}${p.git ? "" : " · no git"}</span><button class="btn small" type="button" data-add="${esc(p.path)}">Add</button></li>`).join("");
    $("#addAll").onclick = () => { addProjects(found.map(p => p.path)); toast(`Added ${found.length} project(s)`); };
    $("#bList").querySelectorAll("[data-add]").forEach(n => n.onclick = () => { addProjects([n.dataset.add]); toast("Added " + n.dataset.add); });
  } catch (e) { toast(e.message); }
}
$("#browseBtn").onclick = () => browseTo(store.get("browse", META.cwd));
$("#upBtn").onclick = () => browseTo($("#upBtn").dataset.path);
$("#addHere").onclick = () => { if ($("#bPath").dataset.path) addProjects([$("#bPath").dataset.path]); };
$("#discoverHere").onclick = () => discoverIn($("#bPath").dataset.path || META.cwd);
$("#discoverBtn").onclick = () => { const p = prompt("Folder to search for Maven/Gradle projects (3 levels deep):", store.get("browse", META.cwd)); if (p) discoverIn(p); };
$("#newForm").addEventListener("submit", async e => {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const cmd = cmdValue(), options = collectOptions(), projects = projectList();
  store.set("cmd", cmd); store.set("opts." + cmd, options); store.set("copy", $("#copy").checked);
  if (!projects.length) { $("#formErr").textContent = "Add at least one project directory."; return; }
  try {
    const jobs = await api("/api/jobs", { method: "POST", body: JSON.stringify({ command: cmd, projects, options, copy: $("#copy").checked }) });
    $("#dlg").close(); toast(`Queued ${jobs.length} job${jobs.length > 1 ? "s" : ""}`);
    await refreshJobs(); select(jobs[0].id);
  } catch (err) { $("#formErr").textContent = err.message; }
});

// ---------------- settings + boot
$("#parallel").onchange = async e => { const r = await api("/api/settings", { method: "POST", body: JSON.stringify({ parallel: Number(e.target.value) }) }); toast(r.parallel === 1 ? "Jobs run one after the other" : `Up to ${r.parallel} jobs run at the same time (they share ~/.m2)`); };
(async () => {
  META = await api("/api/meta");
  $("#ver").textContent = "v" + META.version + " · runs locally · state in " + META.stateDir;
  $("#parallel").value = String(Math.min(4, META.parallel));
  TAB = store.get("tab", "log");
  await refreshJobs();
  if (SEL && JOBS.find(j => j.id === SEL)) select(SEL);
  setInterval(refreshJobs, 2500);
  // keep running durations moving between server events
  setInterval(() => { const j = JOBS.find(x => x.id === SEL); if (j && ACTIVE.includes(j.status)) updateHeader(j); }, 1000);
})();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())

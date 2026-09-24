#!/usr/bin/env python3
"""Run the test suites a staged Windows tree declares, natively or under wine.

A target declares its suites in <target>/share/logos-tests/*.json; the schema
is in docs/windows-ci.md. Every suite runs to the end whatever failed before
it, like ctest. Results: one JUnit XML per suite, a log per failed case, and a
Markdown summary (also appended to $GITHUB_STEP_SUMMARY).

  run_tests.py --stage DIR --results DIR [--wine LAUNCHER] [--budget-seconds N]
  run_tests.py --stage DIR --check     validate the manifests, run nothing
  run_tests.py --self-test             prove the verdicts against fake suites
"""
import argparse
import fnmatch
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

IS_WINDOWS = os.name == "nt"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SUITE_KEYS = {"name", "exe", "kind", "isolation", "timeout", "args", "env", "path",
              "cwd", "filter", "skip", "wine_skip", "wine", "jobs"}
MAX_DETAILED_FAILURES = 60
TAIL_LINES = 80


class ManifestError(Exception):
    pass


# ---------------------------------------------------------------- manifests

def safe_relative(path):
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def parse_skips(value, where):
    if not isinstance(value, list):
        raise ManifestError(f"{where}: must be a list of {{pattern, reason}}")
    skips = []
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"pattern", "reason"}
                or not isinstance(item["pattern"], str) or not item["pattern"]
                or not isinstance(item["reason"], str) or not item["reason"].strip()):
            raise ManifestError(f"{where}: each entry needs a pattern and a non-empty reason")
        skips.append((item["pattern"], item["reason"].strip()))
    return skips


def parse_suite(raw, where, stage, target):
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: not an object")
    unknown = sorted(set(raw) - SUITE_KEYS)
    if unknown:
        raise ManifestError(f"{where}: unknown keys {unknown}")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ManifestError(f"{where}: 'name' must match {NAME_RE.pattern}")
    exe = raw.get("exe")
    if not safe_relative(exe):
        raise ManifestError(f"{where}: 'exe' must be a relative path inside the target")
    exe_path = os.path.join(stage, target, *exe.split("/"))
    if not os.path.isfile(exe_path):
        raise ManifestError(f"{where}: {target}/{exe} is not in the staged tree")
    kind = raw.get("kind", "gtest")
    if kind not in ("gtest", "exe"):
        raise ManifestError(f"{where}: 'kind' must be gtest or exe")
    isolation = raw.get("isolation", "case" if kind == "gtest" else "suite")
    if isolation not in ("case", "suite") or (kind == "exe" and isolation != "suite"):
        raise ManifestError(f"{where}: 'isolation' must be case or suite (exe: suite)")
    timeout = raw.get("timeout", 60 if isolation == "case" else 900)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ManifestError(f"{where}: 'timeout' must be a positive number of seconds")
    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ManifestError(f"{where}: 'args' must be a list of strings")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and ENV_RE.match(k) and isinstance(v, str) for k, v in env.items()):
        raise ManifestError(f"{where}: 'env' must map variable names to strings")
    path = raw.get("path", [])
    if not isinstance(path, list) or not all(isinstance(p, str) and p for p in path):
        raise ManifestError(f"{where}: 'path' must be a list of directories")
    cwd = raw.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise ManifestError(f"{where}: 'cwd' must be a string")
    gtest_filter = raw.get("filter", "")
    if not isinstance(gtest_filter, str) or (gtest_filter and kind != "gtest"):
        raise ManifestError(f"{where}: 'filter' is a gtest filter string (gtest only)")
    wine = raw.get("wine", True)
    if not isinstance(wine, bool):
        raise ManifestError(f"{where}: 'wine' must be true or false")
    jobs = raw.get("jobs", 1)
    if isinstance(jobs, bool) or not isinstance(jobs, int) or not 1 <= jobs <= 16:
        raise ManifestError(f"{where}: 'jobs' must be an integer from 1 to 16")
    return {
        "target": target, "name": name, "exe": exe, "exe_path": exe_path, "kind": kind,
        "isolation": isolation, "timeout": float(timeout), "args": args, "env": env,
        "path": path, "cwd": cwd, "filter": gtest_filter, "wine": wine, "jobs": jobs,
        "skip": parse_skips(raw.get("skip", []), f"{where}.skip"),
        "wine_skip": parse_skips(raw.get("wine_skip", []), f"{where}.wine_skip"),
    }


def load_suites(stage):
    manifests = sorted(glob.glob(os.path.join(stage, "*", "share", "logos-tests", "*.json")))
    if not manifests:
        raise ManifestError(
            f"no test manifest under {stage}/<target>/share/logos-tests/. A test leg "
            "with nothing to run is not a pass; install one from the target's build.")
    suites = []
    for manifest in manifests:
        target = os.path.relpath(manifest, stage).split(os.sep)[0]
        try:
            with open(manifest, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError) as error:
            raise ManifestError(f"{manifest}: {error}")
        if (not isinstance(document, dict) or set(document) != {"suites"}
                or not isinstance(document["suites"], list) or not document["suites"]):
            raise ManifestError(f"{manifest}: expected {{\"suites\": [...]}} with at least one suite")
        for index, raw in enumerate(document["suites"]):
            suites.append(parse_suite(raw, f"{manifest}: suites[{index}]", stage, target))
    seen = set()
    for suite in suites:
        key = (suite["target"], suite["name"])
        if key in seen:
            raise ManifestError(f"suite {key[0]}/{key[1]} is declared twice")
        seen.add(key)
    return manifests, suites


def verify_round_trip(stage):
    """The PE set recorded on the builder must be the one that arrived."""
    manifest = os.path.join(stage, "pe-manifest.txt")
    if not os.path.isfile(manifest):
        raise ManifestError(f"{manifest} is missing: this is not the artifact the build uploaded")
    with open(manifest, encoding="utf-8") as handle:
        recorded = sorted(line.strip().replace("\r", "") for line in handle if line.strip())
    arrived = sorted(
        os.path.relpath(p, stage).replace(os.sep, "/")
        for p in glob.glob(os.path.join(stage, "**", "*"), recursive=True)
        if os.path.isfile(p) and p.lower().endswith((".exe", ".dll")))
    lost = sorted(set(recorded) - set(arrived))
    extra = sorted(set(arrived) - set(recorded))
    if lost or extra:
        raise ManifestError("the PE set changed between upload and download: "
                            f"lost {lost[:20]}, appeared {extra[:20]}")
    return len(arrived)


# ---------------------------------------------------------------- execution

class Context:
    def __init__(self, stage, results, launcher=None, budget=None):
        self.stage = os.path.abspath(stage)
        self.results = os.path.abspath(results)
        self.launcher = launcher
        self.wine = launcher is not None
        self.deadline = time.monotonic() + budget if budget else None
        self.work = tempfile.mkdtemp(prefix="logos-tests-")
        self.base_env = dict(os.environ)
        self.wineserver = None
        if self.wine:
            server = os.path.join(os.path.dirname(launcher), "wineserver")
            self.wineserver = server if os.path.exists(server) else None

    def pe_path(self, host_path):
        """A host path spelled the way the Windows program sees it."""
        path = os.path.abspath(host_path)
        return "Z:" + path.replace("/", "\\") if self.wine else path

    def budget_left(self):
        return self.deadline is None or time.monotonic() < self.deadline


def expand(value, suite, ctx, tmp):
    target = os.path.join(ctx.stage, suite["target"])
    for key, path in (("{stage}", ctx.stage), ("{target}", target), ("{tmp}", tmp),
                      ("{exe_dir}", os.path.dirname(suite["exe_path"]))):
        value = value.replace(key, ctx.pe_path(path))
    return value


def host_dir(value, suite, ctx, tmp):
    """Like expand, but a host path, for the child's working directory."""
    target = os.path.join(ctx.stage, suite["target"])
    for key, path in (("{stage}", ctx.stage), ("{target}", target), ("{tmp}", tmp),
                      ("{exe_dir}", os.path.dirname(suite["exe_path"]))):
        value = value.replace(key, path)
    return value if os.path.isabs(value) else os.path.join(target, *value.split("/"))


def suite_env(suite, ctx, tmp):
    env = dict(ctx.base_env)
    for key, value in suite["env"].items():
        env[key] = expand(value, suite, ctx, tmp)
    extra = [expand(p, suite, ctx, tmp) for p in suite["path"]]
    if extra:
        if ctx.wine:
            env["WINEPATH"] = ";".join(extra + ([env["WINEPATH"]] if env.get("WINEPATH") else []))
        else:
            env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def kill_tree(process, ctx):
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    if ctx.wine and ctx.wineserver:
        # Wine children may have left the process group; the server knows them all.
        subprocess.run([ctx.wineserver, "-k"], env=ctx.base_env, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()


def execute(argv, env, cwd, timeout, ctx, log_path):
    """(exit code or None, timed out, seconds); output goes to log_path."""
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    command = ([ctx.launcher] if ctx.wine else []) + argv
    started = time.monotonic()
    with open(log_path, "wb") as log:
        try:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, env=env, cwd=cwd, **kwargs)
        except OSError as error:
            log.write(f"could not start {command[0]}: {error}\n".encode())
            return None, False, 0.0
        try:
            return process.wait(timeout=timeout), False, time.monotonic() - started
        except subprocess.TimeoutExpired:
            kill_tree(process, ctx)
            return process.returncode, True, time.monotonic() - started


def describe_exit(code):
    if code is None:
        return "did not start"
    if code >= 0xC0000000 or code < 0:
        return f"exited 0x{code & 0xFFFFFFFF:08X}"
    return f"exited {code}"


def tail(path, lines=TAIL_LINES):
    try:
        with open(path, "rb") as handle:
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def skip_reason(case, suite, ctx):
    if case.split(".", 1)[0].startswith("DISABLED_") or ".DISABLED_" in case:
        return "disabled in the source"
    for pattern, reason in suite["skip"] + (suite["wine_skip"] if ctx.wine else []):
        if fnmatch.fnmatchcase(case, pattern):
            return reason
    return None


def list_cases(suite, ctx):
    tmp = tempfile.mkdtemp(prefix="list-", dir=ctx.work)
    log = os.path.join(tmp, "list.log")
    argv = [suite["exe_path"], "--gtest_list_tests"]
    if suite["filter"]:
        argv.append("--gtest_filter=" + suite["filter"])
    cwd = host_dir(suite["cwd"], suite, ctx, tmp) if suite["cwd"] else os.path.dirname(suite["exe_path"])
    code, timed_out, _ = execute(argv, suite_env(suite, ctx, tmp), cwd,
                                 max(120.0, suite["timeout"]), ctx, log)
    if timed_out or code != 0:
        raise RuntimeError(f"--gtest_list_tests {describe_exit(code) if not timed_out else 'timed out'}"
                           f"\n{tail(log)}")
    cases, group = [], None
    for line in tail(log, 1000000).splitlines():
        body = line.split("#", 1)[0].rstrip()
        if not body.strip():
            continue
        if not line.startswith(" "):
            group = body.strip() if body.strip().endswith(".") else None
        elif group:
            cases.append(group + body.strip())
    return cases


def gtest_verdict(case, xml_path):
    """(status, message) from gtest's XML for one case, or None if absent."""
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return None
    for element in root.iter("testcase"):
        if f"{element.get('classname')}.{element.get('name')}" != case:
            continue
        failures = element.findall("failure") + element.findall("error")
        if failures:
            return "failed", (failures[0].get("message") or failures[0].text or "failed").strip()
        if element.get("result") == "skipped" or element.find("skipped") is not None:
            skipped = element.find("skipped")
            return "skipped", ((skipped.get("message") if skipped is not None else "") or "skipped").strip()
        if element.get("status") == "notrun":
            return None
        return "passed", ""
    return None


def run_case(suite, case, ctx):
    tmp = tempfile.mkdtemp(prefix="case-", dir=ctx.work)
    log = os.path.join(tmp, "output.log")
    xml_path = os.path.join(tmp, "result.xml")
    argv = [suite["exe_path"], "--gtest_filter=" + case,
            "--gtest_output=xml:" + ctx.pe_path(xml_path)]
    argv += [expand(a, suite, ctx, tmp) for a in suite["args"]]
    cwd = host_dir(suite["cwd"], suite, ctx, tmp) if suite["cwd"] else os.path.dirname(suite["exe_path"])
    code, timed_out, seconds = execute(argv, suite_env(suite, ctx, tmp), cwd,
                                       suite["timeout"], ctx, log)
    verdict = None if timed_out else gtest_verdict(case, xml_path)
    if timed_out:
        status, message = "failed", f"timed out after {suite['timeout']:g} s"
    elif verdict is None:
        status, message = "failed", f"{describe_exit(code)} without recording a result for the case"
    elif verdict[0] != "failed" and code != 0:
        status, message = "failed", f"the case {verdict[0]} but the process {describe_exit(code)}"
    else:
        status, message = verdict
    return {"case": case, "status": status, "message": message, "seconds": seconds,
            "output": tail(log) if status == "failed" else ""}


def run_whole(suite, ctx, cases):
    """isolation=suite: one process. gtest cases come from its XML."""
    tmp = tempfile.mkdtemp(prefix="suite-", dir=ctx.work)
    log = os.path.join(tmp, "output.log")
    xml_path = os.path.join(tmp, "result.xml")
    argv = [suite["exe_path"]]
    skipped = {c: skip_reason(c, suite, ctx) for c in cases}
    runnable = [c for c in cases if not skipped[c]]
    if suite["kind"] == "gtest":
        if not runnable:
            return [{"case": c, "status": "skipped", "message": skipped[c], "seconds": 0.0,
                     "output": ""} for c in cases]
        argv += ["--gtest_filter=" + ":".join(runnable), "--gtest_output=xml:" + ctx.pe_path(xml_path)]
    argv += [expand(a, suite, ctx, tmp) for a in suite["args"]]
    cwd = host_dir(suite["cwd"], suite, ctx, tmp) if suite["cwd"] else os.path.dirname(suite["exe_path"])
    code, timed_out, seconds = execute(argv, suite_env(suite, ctx, tmp), cwd,
                                       suite["timeout"], ctx, log)
    output = tail(log)
    if suite["kind"] == "exe":
        if timed_out:
            status, message = "failed", f"timed out after {suite['timeout']:g} s"
        elif code != 0:
            status, message = "failed", describe_exit(code)
        else:
            status, message = "passed", ""
        return [{"case": suite["name"], "status": status, "message": message,
                 "seconds": seconds, "output": output if status == "failed" else ""}]
    results = []
    for case in cases:
        if skipped[case]:
            results.append({"case": case, "status": "skipped", "message": skipped[case],
                            "seconds": 0.0, "output": ""})
            continue
        verdict = gtest_verdict(case, xml_path)
        if verdict is None:
            why = f"timed out after {suite['timeout']:g} s" if timed_out else describe_exit(code)
            results.append({"case": case, "status": "failed", "seconds": 0.0, "output": output,
                            "message": f"no result: the suite {why} before recording it"})
        else:
            results.append({"case": case, "status": verdict[0], "message": verdict[1],
                            "seconds": 0.0, "output": output if verdict[0] == "failed" else ""})
    if not timed_out and code != 0 and all(r["status"] != "failed" for r in results):
        results.append({"case": f"{suite['name']}.process", "status": "failed", "seconds": seconds,
                        "message": f"every case passed but the process {describe_exit(code)}",
                        "output": output})
    return results


def run_suite(suite, ctx):
    started = time.monotonic()
    if ctx.wine and not suite["wine"]:
        return {"suite": suite, "results": [], "seconds": 0.0,
                "note": "not run under wine (the manifest says so)"}
    try:
        cases = list_cases(suite, ctx) if suite["kind"] == "gtest" else []
    except RuntimeError as error:
        return {"suite": suite, "seconds": time.monotonic() - started, "note": "",
                "results": [{"case": f"{suite['name']}.list", "status": "failed", "seconds": 0.0,
                             "message": "could not list the suite's cases", "output": str(error)}]}
    if suite["kind"] == "gtest" and not cases:
        return {"suite": suite, "seconds": time.monotonic() - started, "note": "",
                "results": [{"case": f"{suite['name']}.list", "status": "failed", "seconds": 0.0,
                             "message": "the suite lists no cases: nothing ran", "output": ""}]}
    if suite["isolation"] == "suite":
        results = run_whole(suite, ctx, cases)
    else:
        def one(case):
            reason = skip_reason(case, suite, ctx)
            if reason:
                return {"case": case, "status": "skipped", "message": reason, "seconds": 0.0,
                        "output": ""}
            if not ctx.budget_left():
                return {"case": case, "status": "failed", "seconds": 0.0, "output": "",
                        "message": "not run: the job's time budget ran out"}
            result = run_case(suite, case, ctx)
            done.append(case)
            if result["status"] == "failed":
                print(f"    FAILED {case}: {result['message']}", flush=True)
            if len(done) % 50 == 0:
                print(f"    {len(done)}/{len(cases)} cases", flush=True)
            return result
        done = []
        jobs = 1 if ctx.wine else suite["jobs"]
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(one, cases))
    return {"suite": suite, "results": results, "seconds": time.monotonic() - started, "note": ""}


# ---------------------------------------------------------------- reporting

def write_junit(report, ctx):
    suite = report["suite"]
    results = report["results"]
    directory = os.path.join(ctx.results, suite["target"])
    os.makedirs(directory, exist_ok=True)
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("failed", "skipped")}
    root = ET.Element("testsuites", name=f"{suite['target']}/{suite['name']}")
    node = ET.SubElement(root, "testsuite", name=f"{suite['target']}/{suite['name']}",
                         tests=str(len(results)), failures=str(counts["failed"]),
                         skipped=str(counts["skipped"]), time=f"{report['seconds']:.3f}")
    for result in results:
        group, _, name = result["case"].rpartition(".")
        case = ET.SubElement(node, "testcase", classname=group or suite["name"],
                             name=name or result["case"], time=f"{result['seconds']:.3f}")
        if result["status"] == "failed":
            failure = ET.SubElement(case, "failure", message=result["message"])
            failure.text = result["output"]
        elif result["status"] == "skipped":
            ET.SubElement(case, "skipped", message=result["message"])
    ET.ElementTree(root).write(os.path.join(directory, f"{suite['name']}.xml"),
                               encoding="utf-8", xml_declaration=True)
    for result in results:
        if result["status"] == "failed" and result["output"]:
            logs = os.path.join(directory, suite["name"])
            os.makedirs(logs, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", result["case"])
            with open(os.path.join(logs, f"{safe}.log"), "w", encoding="utf-8") as handle:
                handle.write(result["output"])


def summary(reports, ctx, heading):
    lines = [f"### {heading}", "",
             "| suite | cases | passed | failed | skipped | time |",
             "|---|---:|---:|---:|---:|---:|"]
    failures = []
    for report in reports:
        suite, results = report["suite"], report["results"]
        count = {s: sum(1 for r in results if r["status"] == s)
                 for s in ("passed", "failed", "skipped")}
        label = f"`{suite['target']}/{suite['name']}`"
        if report["note"]:
            lines.append(f"| {label} | — | — | — | — | {report['note']} |")
            continue
        lines.append(f"| {label} | {len(results)} | {count['passed']} | {count['failed']} | "
                     f"{count['skipped']} | {report['seconds']:.1f} s |")
        failures += [(suite, r) for r in results if r["status"] == "failed"]
    if failures:
        lines += ["", f"**{len(failures)} failed**", ""]
        for suite, result in failures[:MAX_DETAILED_FAILURES]:
            lines.append(f"- `{suite['target']}/{suite['name']}` `{result['case']}`: "
                         f"{result['message'].splitlines()[0] if result['message'] else 'failed'}")
            if result["output"]:
                body = "\n".join(result["output"].splitlines()[-30:]).replace("```", "'''")
                lines += ["  <details><summary>output</summary>", "", "  ```",
                          *("  " + l for l in body.splitlines()), "  ```", "  </details>"]
        if len(failures) > MAX_DETAILED_FAILURES:
            lines.append(f"- … {len(failures) - MAX_DETAILED_FAILURES} more in the JUnit files")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- platform setup

def prepare_platform(ctx):
    if IS_WINDOWS:
        import ctypes
        # No crash or missing-DLL dialog may wait for a click; children inherit this.
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    if ctx.wine:
        env = ctx.base_env
        env.setdefault("WINEPREFIX", os.path.join(ctx.work, "wineprefix"))
        env["WINEDEBUG"] = "-all"
        env["WINEDLLOVERRIDES"] = "mscoree,mshtml="
        for key in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
            env.pop(key, None)
        # Created once here, not inside the first case's timeout.
        subprocess.run([ctx.launcher, "wineboot", "--init"], env=env, timeout=600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ctx.wineserver:
            subprocess.run([ctx.wineserver, "-p"], env=env, timeout=60)


def finish_platform(ctx):
    if ctx.wine and ctx.wineserver:
        subprocess.run([ctx.wineserver, "-k"], env=ctx.base_env, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(stage, results, launcher, budget, heading):
    ctx = Context(stage, results, launcher, budget)
    os.makedirs(ctx.results, exist_ok=True)
    try:
        count = verify_round_trip(ctx.stage)
        print(f"artifact round trip verified: {count} PEs")
        _, suites = load_suites(ctx.stage)
    except ManifestError as error:
        print(f"::error::{error}")
        return 1
    prepare_platform(ctx)
    reports = []
    try:
        for suite in suites:
            print(f"--- {suite['target']}/{suite['name']} ({suite['kind']}, {suite['isolation']})",
                  flush=True)
            report = run_suite(suite, ctx)
            write_junit(report, ctx)
            failed = [r for r in report["results"] if r["status"] == "failed"]
            for result in failed:
                print(f"::error::{suite['target']}/{suite['name']} {result['case']}: "
                      f"{result['message']}")
            print(f"    {len(report['results'])} cases, {len(failed)} failed, "
                  f"{report['seconds']:.1f} s {report['note']}", flush=True)
            reports.append(report)
    finally:
        finish_platform(ctx)
        shutil.rmtree(ctx.work, ignore_errors=True)
    text = summary(reports, ctx, heading)
    with open(os.path.join(ctx.results, "summary.md"), "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 1 if any(r["status"] == "failed" for rep in reports for r in rep["results"]) else 0


def check(stage):
    try:
        manifests, suites = load_suites(stage)
    except ManifestError as error:
        print(f"::error::{error}")
        return 1
    print(f"{len(manifests)} manifest(s), {len(suites)} suite(s):")
    for suite in suites:
        print(f"  {suite['target']}/{suite['name']}: {suite['exe']} ({suite['kind']}, "
              f"{suite['isolation']}, {suite['timeout']:g} s, wine={suite['wine']})")
    return 0


# ---------------------------------------------------------------- self-test

FAKE_GTEST = r'''#!/usr/bin/env python3
import fnmatch, sys, time
CASES = {"Math.Adds": "pass", "Math.Fails": "fail", "Math.Skips": "skip",
         "Crash.Dies": "crash", "Hang.Forever": "hang", "Exit.AfterPass": "exitafter",
         "Param/Grid.Cell/0": "pass", "Known.Broken": "fail", "Math.DISABLED_Old": "pass"}
args = sys.argv[1:]
flt = next((a.split("=", 1)[1] for a in args if a.startswith("--gtest_filter=")), "*")
selected = [c for c in CASES if any(fnmatch.fnmatchcase(c, p) for p in flt.split("-")[0].split(":") if p)]
if "--gtest_list_tests" in args:
    groups = {}
    for case in selected:
        group, name = case.rsplit(".", 1)
        groups.setdefault(group, []).append(name)
    print("Running main() from gtest_main.cc")
    for group, names in groups.items():
        print(group + ".")
        for name in names:
            print("  " + name + ("  # GetParam() = 3" if "/" in name else ""))
    sys.exit(0)
xml = [a.split("xml:", 1)[1] for a in args if a.startswith("--gtest_output=xml:")][0]
rows, code = [], 0
for case in selected:
    kind = CASES[case]
    if kind == "crash":
        sys.exit(3)
    if kind == "hang":
        time.sleep(600)
    group, name = case.rsplit(".", 1)
    body = ""
    if kind == "fail":
        body, code = '<failure message="Expected 1, got 2" type=""/>', 1
    if kind == "skip":
        body = '<skipped message="needs a GPU"/>'
    result = "skipped" if kind == "skip" else "completed"
    rows.append(f'<testcase name="{name}" classname="{group}" status="run" result="{result}">{body}</testcase>')
    if kind == "exitafter":
        code = 7
with open(xml, "w") as handle:
    handle.write("<testsuites><testsuite>" + "".join(rows) + "</testsuite></testsuites>")
print("ran", selected)
sys.exit(code)
'''

FAKE_EXE = r'''#!/usr/bin/env python3
import sys
print("probe")
sys.exit(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
'''


def self_test():
    """Every verdict this runner can give, against fake suites, on Linux or macOS."""
    root = tempfile.mkdtemp(prefix="run-tests-self-")
    stage = os.path.join(root, "stage")
    bin_dir = os.path.join(stage, "fake", "bin")
    os.makedirs(os.path.join(stage, "fake", "share", "logos-tests"))
    os.makedirs(bin_dir)
    for name, body in (("fake_tests.exe", FAKE_GTEST), ("probe.exe", FAKE_EXE),
                       ("empty_tests.exe", "#!/usr/bin/env python3\nprint('Running main()')\n")):
        path = os.path.join(bin_dir, name)
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o755)
    manifest = {"suites": [
        {"name": "fake", "exe": "bin/fake_tests.exe", "timeout": 3,
         "skip": [{"pattern": "Known.*", "reason": "tracked in #1"}]},
        {"name": "fake_whole", "exe": "bin/fake_tests.exe", "isolation": "suite", "timeout": 5,
         "filter": "Math.*:Param/*"},
        {"name": "fake_whole_crash", "exe": "bin/fake_tests.exe", "isolation": "suite",
         "timeout": 5, "filter": "Math.Adds:Crash.*"},
        {"name": "probe_ok", "exe": "bin/probe.exe", "kind": "exe", "timeout": 10},
        {"name": "probe_bad", "exe": "bin/probe.exe", "kind": "exe", "args": ["4"], "timeout": 10},
        {"name": "empty", "exe": "bin/empty_tests.exe", "timeout": 10},
    ]}
    with open(os.path.join(stage, "fake", "share", "logos-tests", "fake.json"), "w") as handle:
        json.dump(manifest, handle)
    with open(os.path.join(stage, "pe-manifest.txt"), "w") as handle:
        handle.write("fake/bin/empty_tests.exe\nfake/bin/fake_tests.exe\nfake/bin/probe.exe\n")
    results = os.path.join(root, "results")
    saved = os.environ.pop("GITHUB_STEP_SUMMARY", None)
    try:
        code = run(stage, results, None, None, "self-test")
    finally:
        if saved is not None:
            os.environ["GITHUB_STEP_SUMMARY"] = saved

    def verdicts(name):
        tree = ET.parse(os.path.join(results, "fake", f"{name}.xml")).getroot()
        out = {}
        for case in tree.iter("testcase"):
            state = "failed" if case.find("failure") is not None else (
                "skipped" if case.find("skipped") is not None else "passed")
            detail = case.find("failure") if state == "failed" else case.find("skipped")
            out[f"{case.get('classname')}.{case.get('name')}"] = (
                state, detail.get("message") if detail is not None else "")
        return out

    expect = {
        "fake": {
            "Math.Adds": ("passed", ""), "Math.Fails": ("failed", "Expected 1"),
            "Math.Skips": ("skipped", "needs a GPU"), "Crash.Dies": ("failed", "without recording"),
            "Hang.Forever": ("failed", "timed out"), "Exit.AfterPass": ("failed", "exited 7"),
            "Param/Grid.Cell/0": ("passed", ""), "Known.Broken": ("skipped", "tracked in #1"),
            "Math.DISABLED_Old": ("skipped", "disabled"),
        },
        "fake_whole": {
            "Math.Adds": ("passed", ""), "Math.Fails": ("failed", "Expected 1"),
            "Math.Skips": ("skipped", "needs a GPU"), "Param/Grid.Cell/0": ("passed", ""),
            "Math.DISABLED_Old": ("skipped", "disabled"),
        },
        "fake_whole_crash": {
            "Math.Adds": ("failed", "no result: the suite exited 3"),
            "Crash.Dies": ("failed", "no result: the suite exited 3"),
        },
        "probe_ok": {"probe_ok.probe_ok": ("passed", "")},
        "probe_bad": {"probe_bad.probe_bad": ("failed", "exited 4")},
        "empty": {"empty.list": ("failed", "lists no cases")},
    }
    bad = 0
    for name, cases in expect.items():
        got = verdicts(name)
        for case, (state, fragment) in cases.items():
            actual = got.get(case)
            if actual is None or actual[0] != state or fragment not in (actual[1] or ""):
                print(f"::error::self-test: {name} {case}: expected {state} ({fragment!r}), got {actual}")
                bad = 1
        extra = sorted(set(got) - set(cases))
        if extra:
            print(f"::error::self-test: {name} reported unexpected cases {extra}")
            bad = 1
    if code != 1:
        print(f"::error::self-test: a run with failures exited {code}, not 1")
        bad = 1
    # A manifest that names a missing exe is refused before anything runs.
    os.remove(os.path.join(bin_dir, "probe.exe"))
    if check(stage) != 1:
        print("::error::self-test: a manifest naming a missing exe passed --check")
        bad = 1
    shutil.rmtree(os.path.join(stage, "fake", "share"))
    if check(stage) != 1:
        print("::error::self-test: a stage with no manifest passed --check")
        bad = 1
    shutil.rmtree(root, ignore_errors=True)
    print("self-test " + ("FAILED" if bad else "passed: every verdict is the expected one"))
    return bad


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", default="stage")
    parser.add_argument("--results", default="test-results")
    parser.add_argument("--wine", metavar="LAUNCHER", help="run every PE through this wine launcher")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="stop starting cases after this long, so the report still gets written")
    parser.add_argument("--heading", default=None)
    parser.add_argument("--check", action="store_true", help="validate the manifests only")
    parser.add_argument("--self-test", action="store_true")
    options = parser.parse_args()
    if options.self_test:
        return self_test()
    if options.check:
        return check(options.stage)
    heading = options.heading or ("Windows tests (wine)" if options.wine else "Windows tests (native)")
    return run(options.stage, options.results, options.wine, options.budget_seconds, heading)


if __name__ == "__main__":
    sys.exit(main())

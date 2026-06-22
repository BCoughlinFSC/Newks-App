"""
integrations_agent.py — RUNS ON dev-jan02 (not the Cloudways app).

Polls the Cloudways app for queued integration jobs, runs the allowlisted command
locally (from its own working dir), and posts the result back. Outbound-only
(dev-jan02 -> Cloudways HTTPS) — no inbound access to dev-jan02 needed. One job
at a time.

Config via env:
  INTEGRATIONS_APP_URL      e.g. https://<your-cloudways-domain>
  INTEGRATIONS_AGENT_TOKEN  shared secret — MUST match the app's env var
  INTEGRATIONS_POLL_SECONDS optional, default 10
  INTEGRATIONS_JOB_TIMEOUT  optional per-job cap (s), default 5400 (90 min)
"""
import os
import re
import subprocess
import sys
import time

import httpx

APP_URL = os.getenv("INTEGRATIONS_APP_URL", "").rstrip("/")
AGENT_TOKEN = os.getenv("INTEGRATIONS_AGENT_TOKEN", "")
POLL_SECONDS = int(os.getenv("INTEGRATIONS_POLL_SECONDS", "10"))
JOB_TIMEOUT = int(os.getenv("INTEGRATIONS_JOB_TIMEOUT", "5400"))

# ── ALLOWLIST — the ONLY commands this agent will ever run. ───────────────────
# The cloud sends only a job_key (+ optional date); the command is resolved HERE.
#   cmd      : argv list, run from `cwd`
#   cwd      : working directory
#   date_arg : if True, a validated YYYY-MM-DD is appended to cmd
VENV_PY = "/opt/toast_backfill/backend/venv/bin/python"
JOBS = {
    "sysco_invoices_dryrun": {"cmd": ["python3", "sysco_ftp.py", "invoices", "--dry-run"], "cwd": "/opt/sysco"},
    "sysco_invoices":        {"cmd": ["python3", "sysco_ftp.py", "invoices"],              "cwd": "/opt/sysco"},
    "sysco_pos_dryrun":      {"cmd": ["python3", "sysco_ftp.py", "pos", "--dry-run"],       "cwd": "/opt/sysco"},
    "sysco_pos":             {"cmd": ["python3", "sysco_ftp.py", "pos"],                    "cwd": "/opt/sysco"},
    "repoll_sales":          {"cmd": [VENV_PY, "nightly_pull.py"], "cwd": "/opt/toast_backfill/backend", "date_arg": True},
    # Add once confirmed:
    # "repoll_labor":        {"cmd": [VENV_PY, "<labor_script>.py"], "cwd": "/opt/toast_backfill/backend", "date_arg": True},
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _headers():
    return {"X-Agent-Token": AGENT_TOKEN}


def run_job(job_key: str, args: str):
    """Run an allowlisted command; return (status, exit_code, log)."""
    spec = JOBS.get(job_key)
    if not spec:
        return "failed", None, f"Unknown job_key '{job_key}' — not in agent allowlist."
    cmd = list(spec["cmd"])
    if spec.get("date_arg"):
        if not _DATE_RE.match(args or ""):
            return "failed", None, f"This job needs a date (YYYY-MM-DD); got '{args}'."
        cmd.append(args)
    header = f"$ {' '.join(cmd)}   (cwd={spec.get('cwd')})\n\n"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=JOB_TIMEOUT, cwd=spec.get("cwd"))
        log = header + (proc.stdout or "")
        if proc.stderr:
            log += "\n--- STDERR ---\n" + proc.stderr
        return ("success" if proc.returncode == 0 else "failed"), proc.returncode, log
    except subprocess.TimeoutExpired:
        return "failed", None, header + f"Timed out after {JOB_TIMEOUT}s."
    except Exception as exc:  # noqa: BLE001
        return "failed", None, header + f"Agent exception: {exc}"


def main():
    if not APP_URL or not AGENT_TOKEN:
        sys.exit("Set INTEGRATIONS_APP_URL and INTEGRATIONS_AGENT_TOKEN env vars first.")
    print(f"[agent] polling {APP_URL} every {POLL_SECONDS}s — {len(JOBS)} jobs in allowlist", flush=True)
    with httpx.Client(timeout=60.0) as client:
        while True:
            try:
                resp = client.get(f"{APP_URL}/api/integrations/agent/next", headers=_headers())
                resp.raise_for_status()
                job = resp.json()
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] poll error: {exc}", flush=True)
                time.sleep(POLL_SECONDS)
                continue

            if not job or not job.get("id"):
                time.sleep(POLL_SECONDS)
                continue

            jid, key, args = job["id"], job["job_key"], job.get("args", "")
            print(f"[agent] running job {jid}: {key} {args}".rstrip(), flush=True)
            status, code, log = run_job(key, args)
            try:
                client.post(
                    f"{APP_URL}/api/integrations/agent/result",
                    headers=_headers(),
                    json={"id": jid, "status": status, "exit_code": code, "log": log},
                )
                print(f"[agent] job {jid} -> {status} (exit={code})", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] FAILED to post result for job {jid}: {exc}", flush=True)


if __name__ == "__main__":
    main()

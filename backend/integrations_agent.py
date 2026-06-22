"""
integrations_agent.py — RUNS ON dev-jan02 (not the Cloudways app).

Polls the Cloudways app for queued integration jobs, runs the allowlisted script
locally, and posts the result back. Outbound-only (dev-jan02 -> Cloudways HTTPS),
so no inbound access to dev-jan02 is needed. One job at a time.

Config via env:
  INTEGRATIONS_APP_URL      e.g. https://<your-cloudways-domain>
  INTEGRATIONS_AGENT_TOKEN  shared secret — MUST match the app's env var
  INTEGRATIONS_POLL_SECONDS optional, default 10

Run (quick test):   INTEGRATIONS_APP_URL=... INTEGRATIONS_AGENT_TOKEN=... python3 integrations_agent.py
Run (persistent):   as a systemd service (see deploy notes).
"""
import os
import subprocess
import sys
import time

import httpx

APP_URL = os.getenv("INTEGRATIONS_APP_URL", "").rstrip("/")
AGENT_TOKEN = os.getenv("INTEGRATIONS_AGENT_TOKEN", "")
POLL_SECONDS = int(os.getenv("INTEGRATIONS_POLL_SECONDS", "10"))
JOB_TIMEOUT = int(os.getenv("INTEGRATIONS_JOB_TIMEOUT", "3600"))  # per-job hard cap (s)

# ── ALLOWLIST — the ONLY commands this agent will ever run. ───────────────────
# The cloud only sends a job_key; the actual command is resolved HERE, locally.
# >>> EDIT these paths to wherever the 3 ported Python scripts live on dev-jan02. <<<
JOBS = {
    "sysco_invoices": ["python3", "/opt/toast_backfill/integrations/get_sysco_invoices.py"],
    "sysco_pos":      ["python3", "/opt/toast_backfill/integrations/get_sysco_pos.py"],
    "esite_send":     ["python3", "/opt/toast_backfill/integrations/send_esite_files.py"],
}


def _headers():
    return {"X-Agent-Token": AGENT_TOKEN}


def run_job(job_key: str):
    """Run an allowlisted command; return (status, exit_code, log)."""
    cmd = JOBS.get(job_key)
    if not cmd:
        return "failed", None, f"Unknown job_key '{job_key}' — not in agent allowlist."
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=JOB_TIMEOUT)
        log = (proc.stdout or "")
        if proc.stderr:
            log += "\n--- STDERR ---\n" + proc.stderr
        return ("success" if proc.returncode == 0 else "failed"), proc.returncode, log
    except subprocess.TimeoutExpired:
        return "failed", None, f"Timed out after {JOB_TIMEOUT}s."
    except Exception as exc:  # noqa: BLE001
        return "failed", None, f"Agent exception running {job_key}: {exc}"


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

            jid, key = job["id"], job["job_key"]
            print(f"[agent] running job {jid}: {key}", flush=True)
            status, code, log = run_job(key)
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

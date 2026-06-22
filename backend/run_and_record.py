"""
run_and_record.py — RUNS ON dev-jan02. Wrap a scheduled (cron) command so its
run shows up in the Integrations dashboard (Recent Runs + last-run badge).

Usage in cron:
  <wrapper> <job_key> [extra] -- <command> [args...]

Examples (run the wrapper with the venv python so httpx is available):
  cd /opt/sysco && /opt/toast_backfill/backend/venv/bin/python \
      /opt/toast_backfill/backend/run_and_record.py sysco_invoices -- python3 sysco_ftp.py invoices

  cd /opt/toast_backfill/backend && /opt/toast_backfill/backend/venv/bin/python \
      run_and_record.py repoll_sales 2026-06-22 -- venv/bin/python nightly_pull.py 2026-06-22

- `<job_key>` should match a key in the dashboard catalog so its badge updates
  (sysco_invoices, sysco_pos, repoll_sales, …). Any other key still shows in Recent Runs.
- Anything between <job_key> and `--` is recorded as `args` (e.g. the date).
- Reporting is BEST-EFFORT: the wrapped command's exit code is preserved, and a
  failed/[missing httpx] report never affects the job.

Env: INTEGRATIONS_APP_URL, INTEGRATIONS_AGENT_TOKEN (same values as the agent).
Put these at the TOP of the crontab (cron doesn't inherit your shell env):
  INTEGRATIONS_APP_URL=https://<your-app-url>
  INTEGRATIONS_AGENT_TOKEN=<same secret as the app .env>
"""
import os
import subprocess
import sys
import time


def main():
    argv = sys.argv[1:]
    if "--" not in argv or argv.index("--") == 0:
        sys.exit("usage: run_and_record.py <job_key> [extra] -- <command> [args...]")
    sep = argv.index("--")
    job_key = argv[0]
    extra = " ".join(argv[1:sep])          # optional, recorded as `args` (e.g. a date)
    cmd = argv[sep + 1:]
    if not cmd:
        sys.exit("no command after --")

    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        out = proc.stdout or ""
        if proc.stderr:
            out += "\n--- STDERR ---\n" + proc.stderr
        code = proc.returncode
    except Exception as exc:  # noqa: BLE001
        out, code = f"wrapper exception: {exc}", 1

    status = "success" if code == 0 else "failed"
    log = f"$ {' '.join(cmd)}   (scheduled, {time.time() - t0:.0f}s)\n\n" + out

    # Mirror the command's output to cron's own log, unchanged.
    sys.stdout.write(out)
    if proc_stderr := (out if code != 0 else ""):
        sys.stderr.write(proc_stderr)

    # Best-effort report to the dashboard — never fail the job over this.
    app_url = os.getenv("INTEGRATIONS_APP_URL", "").rstrip("/")
    token = os.getenv("INTEGRATIONS_AGENT_TOKEN", "")
    if app_url and token:
        try:
            import httpx
            httpx.post(
                f"{app_url}/api/integrations/record",
                headers={"X-Agent-Token": token},
                json={"job_key": job_key, "status": status, "exit_code": code,
                      "log": log, "args": extra, "requested_by": "cron"},
                timeout=30.0,
            )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"\n[run_and_record] report failed (job still ran): {exc}\n")

    sys.exit(code)


if __name__ == "__main__":
    main()

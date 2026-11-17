#!/usr/bin/env python3
"""Run an OpenShift node maintenance action (shutdown/startup) via the
maintenance playbook and email the result.

Usage:
    node-maintenance.py --action shutdown|startup --node control01|control02

Cron schedule (see crontab):
    control01: shutdown Mon 18:00, startup Tue 07:00
    control02: shutdown Tue 18:00, startup Wed 07:00
    control01: shutdown Wed 18:00, startup Thu 07:00
    control02: shutdown Thu 18:00, startup Fri 07:00
    control01: shutdown Fri 18:00, startup Sat 07:00  (control01 down over the weekend)

Exit codes:
    0  playbook succeeded and email was sent
    1  playbook failed (email with error output sent)
    2  email could not be sent after the playbook finished
"""

import argparse
import configparser
import datetime as dt
import os
import subprocess
import sys
from email.message import EmailMessage
from pathlib import Path

REPO = Path("/home/syangsao/openshift_lab_maintenance")
KUBECONFIG = Path.home() / "ocp422-luke" / "auth" / "kubeconfig"
SMTP_CONF = REPO / "node-maintenance.smtp.conf"
VAULT_PASS_FILE = Path("/tmp/.nm-vaultpass")
LOG_DIR = Path.home() / "openshift_lab_maintenance" / "logs"

NODES = {
    "control01": "control01.syangsao.net",
    "control02": "control02.syangsao.net",
}


def log(msg: str) -> None:
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "node-maintenance.log", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def run_playbook(action: str, node: str) -> tuple[int, str]:
    """Run the maintenance playbook; return (rc, full_output)."""
    cmd = [
        "ansible-playbook",
        str(REPO / "maintenance.yml"),
        "-e", f"@{REPO / f'{node}.yml'}",
        "-e", f"action={action}",
        "--vault-password-file", str(VAULT_PASS_FILE),
    ]
    env = dict(os.environ, KUBECONFIG=str(KUBECONFIG))
    log(f"running: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True)
    out = proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")
    return proc.returncode, out


def send_email(subject: str, body: str) -> None:
    cfg = configparser.ConfigParser()
    cfg.read(SMTP_CONF)
    smtp = cfg["smtp"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp["from"]
    msg["To"] = smtp["to"]
    msg.set_content(body)

    import smtplib
    if smtp.get("secure", "tls") == "ssl":
        server = smtplib.SMTP_SSL(smtp["host"], int(smtp["port"]), timeout=30)
    else:
        server = smtplib.SMTP(smtp["host"], int(smtp["port"]), timeout=30)
    try:
        server.ehlo()
        if smtp.get("secure", "tls") == "tls":
            server.starttls()
            server.ehlo()
        user, pwd = smtp.get("username"), smtp.get("password")
        if user and pwd:
            server.login(user, pwd)
        server.sendmail(msg["From"], [msg["To"]], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", required=True, choices=["shutdown", "startup"])
    parser.add_argument("--node", required=True, choices=sorted(NODES))
    args = parser.parse_args()

    action, node = args.action, args.node
    node_name = NODES[node]
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    if not VAULT_PASS_FILE.exists():
        log(f"ERROR: vault password file {VAULT_PASS_FILE} is missing; cannot run playbook")
        try:
            send_email(
                f"[node-maintenance] FAILED to start {action} of {node_name}: vault password file missing",
                f"{now}\n\n{VAULT_PASS_FILE} does not exist on the management host.\n"
                "No playbook run was attempted. Restore the file and re-run manually.",
            )
        except Exception as e:
            log(f"ERROR: email also failed: {e}")
        return 1

    rc, out = run_playbook(action, node)
    tail = "\n".join(out.splitlines()[-60:])

    if rc == 0:
        subject = f"[node-maintenance] SUCCESS: {action} of {node_name}"
        body = (
            f"{now}\n\nPlaybook completed successfully.\n\n"
            f"--- playbook output (last 60 lines) ---\n{tail}\n"
        )
    else:
        subject = f"[node-maintenance] FAILED: {action} of {node_name}"
        body = (
            f"{now}\n\nPlaybook failed with exit code {rc}.\n"
            "Inspect the node and iDRAC state before retrying.\n\n"
            f"--- playbook output (last 60 lines) ---\n{tail}\n"
        )

    log(f"playbook rc={rc}; sending email: {subject}")
    try:
        send_email(subject, body)
    except Exception as e:
        log(f"ERROR: failed to send email: {e}")
        return 2

    log(f"done: {action} of {node_name} rc={rc}, email sent")
    return rc


if __name__ == "__main__":
    sys.exit(main())

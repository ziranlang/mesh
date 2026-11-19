#!/usr/bin/env python3
"""Read or reply to app feedback from the owner's developer harness over SSH."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys


REMOTE = r'''
import json, sys, urllib.request, urllib.error
from pathlib import Path
operation = json.load(sys.stdin)
values = {}
for line in Path("/etc/mesh/mesh.env").read_text().splitlines():
    name, separator, value = line.partition("=")
    if separator:
        values[name.strip()] = value.strip().strip("\"'")
developer = operation["action"] in ("developer-inbox", "developer-finish")
name = "MESH_LUMI_DEVELOPER_TOKEN" if developer else "MESH_FEEDBACK_TOKEN"
token = values.get(name, "")
if not token and values.get(name + "_FILE"):
    token = Path(values[name + "_FILE"]).read_text().strip()
if not token and not developer:
    token = values.get("MESH_ADMIN_TOKEN", "")
if not token:
    print("Developer inbox credential is unavailable.", file=sys.stderr)
    sys.exit(1)
base = "http://127.0.0.1:8080/api/v1/admin/"
base += "lumi/developer" if developer else "feedback"
payload = None
if operation["action"] == "reply":
    base += "/reply"
    payload = json.dumps(operation["reply"]).encode()
elif operation["action"] == "developer-finish":
    payload = json.dumps(operation["reply"]).encode()
request = urllib.request.Request(base, data=payload, headers={"X-Mesh-Admin": token, "Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        result = json.load(response)
except urllib.error.HTTPError as error:
    print("Developer inbox: HTTP", error.code, file=sys.stderr)
    sys.exit(1)
except Exception as error:
    print("Developer inbox:", type(error).__name__, file=sys.stderr)
    sys.exit(1)
print(json.dumps(result, ensure_ascii=False, indent=2))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("inbox")
    commands.add_parser("developer-inbox")
    developer = commands.add_parser("developer-finish")
    developer.add_argument("--file", required=True, type=Path)
    reply = commands.add_parser("reply")
    reply.add_argument("--account-id", required=True)
    reply.add_argument("--report-id", required=True)
    reply.add_argument("--reply-file", required=True, type=Path)
    # A reply with its own ID is added beside the report's earlier replies.
    reply.add_argument("--reply-id")
    args = parser.parse_args()
    operation = {"action": args.action}
    if args.action == "developer-finish":
        with args.file.open("rb") as stream:
            document = stream.read(65537)
        if len(document) > 65536:
            parser.error("developer reply document too large")
        operation["reply"] = json.loads(document)
    if args.action == "reply":
        message = args.reply_file.read_text()
        if not message.strip() or len(message.encode()) > 4096:
            parser.error("reply must contain 1 to 4096 bytes")
        operation["reply"] = {"account_id": args.account_id, "id": args.report_id,
                              "reply": message}
        if args.reply_id is not None:
            if len(args.reply_id) != 64 or any(c not in "0123456789abcdef" for c in args.reply_id):
                parser.error("reply ID must be 64 lowercase hexadecimal characters")
            operation["reply"]["reply_id"] = args.reply_id
    result = subprocess.run(
        ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         args.host, "python3 -c " + shlex.quote(REMOTE)],
        input=json.dumps(operation), text=True, capture_output=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

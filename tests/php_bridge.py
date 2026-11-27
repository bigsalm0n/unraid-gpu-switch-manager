"""Use the installable PHP code with a per-call synthetic system adapter."""
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PHP = ["php", "-d", "auto_prepend_file=none", "-d", "display_errors=stderr"]


class Refused(Exception):
    pass


def bridge(root, target, *args, method=None, command=None, **options):
    request = dict(root=str(root), target=target, method=method, args=args, **options)
    with subprocess.Popen([*PHP, str(ROOT / "tests/mock_runtime.php")],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as call:
        def send(value):
            call.stdin.write(json.dumps(value) + "\n")
            call.stdin.flush()

        send(request)
        try:
            for line in call.stdout:
                message = json.loads(line)
                if "command" not in message:
                    if "error" in message:
                        raise Refused(message["error"])
                    return message["result"]
                try:
                    assert command is not None, message
                    reply = dict(output=command(*message["command"], timeout=message["timeout"]))
                except Refused as error:
                    reply = dict(error=str(error))
                send(reply)
            raise AssertionError(call.stderr.read())
        finally:
            call.stdin.close()


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))

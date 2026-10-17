"""Zero-integration entrypoint: ``import remote_ui_auto`` and you're done.

Wraps ``pyray.end_drawing`` so every frame is captured just before the buffer
swap, lazily starts RemoteUI once the window/GL context exists, and launches
``stream_server.py`` as a subprocess. No edits to the render loop are needed.

Set ``OP_REMOTE_UI=0`` in the environment to disable. Any failure here is
swallowed so it can never take the openpilot UI down.
"""

import atexit
import os
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor")
SERVER_PORT = 8081

if os.path.isdir(VENDOR) and VENDOR not in sys.path:
  sys.path.insert(0, VENDOR)
if HERE not in sys.path:
  sys.path.insert(0, HERE)

_remote = None
_server = None
_disabled = os.environ.get("OP_REMOTE_UI", "1") == "0"


def _server_listening() -> bool:
  try:
    with socket.create_connection(("127.0.0.1", SERVER_PORT), timeout=0.2):
      return True
  except OSError:
    return False


def _start_server():
  global _server
  if _server_listening():
    return  # already running (e.g. started by hand or left over)
  env = dict(os.environ)
  env["PYTHONPATH"] = os.pathsep.join(p for p in (VENDOR, HERE, env.get("PYTHONPATH", "")) if p)
  _server = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "stream_server.py")],
    cwd=HERE,
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
  )


def _shutdown():
  global _remote, _server
  if _remote is not None:
    try:
      _remote.close()
    except Exception:
      pass
    _remote = None
  if _server is not None:
    try:
      _server.terminate()
    except Exception:
      pass
    _server = None


def _install():
  global _disabled
  import pyray as pr

  orig_end_drawing = pr.end_drawing

  def end_drawing():
    global _remote, _disabled
    if _remote is None and not _disabled:
      try:
        from remote_ui import RemoteUI

        _remote = RemoteUI()  # window/GL context exists by the first frame
        _start_server()
        atexit.register(_shutdown)
      except Exception as e:
        print(f"remote_ui_auto: disabled ({e})")
        _disabled = True
    if _remote is not None:
      _remote.stream_frame()  # must run before the buffer swap
    orig_end_drawing()

  pr.end_drawing = end_drawing


if not _disabled:
  try:
    _install()
  except Exception as e:
    print(f"remote_ui_auto: failed to install hook ({e})")

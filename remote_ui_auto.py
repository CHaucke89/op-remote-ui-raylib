"""Zero-integration entrypoint: ``import remote_ui_auto`` and you're done.

Wraps ``pyray.end_drawing`` so every frame is captured just before the buffer
swap and launches ``stream_server.py`` as a subprocess. No edits to the render
loop are needed.

Like cloudypilot, nothing is captured or hooked until a browser connects:
``RemoteUI`` is created when ``stream_server`` reports its first client, and
frames are only streamed while at least one client is connected.

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
_client_state = None
_server_started = False
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
  # Give the server the UI's import path too, so it can reach openpilot's cereal for the engaged check.
  paths = [VENDOR, HERE, env.get("PYTHONPATH", ""), *(p for p in sys.path if p and os.path.isdir(p))]
  env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(p for p in paths if p))
  _server = subprocess.Popen(
    [sys.executable, os.path.join(HERE, "stream_server.py")],
    cwd=HERE,
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
  )


def _shutdown():
  global _remote, _server, _client_state
  if _remote is not None:
    try:
      _remote.close()
    except Exception:
      pass
    _remote = None
  if _client_state is not None:
    try:
      _client_state.close()
    except Exception:
      pass
    _client_state = None
  if _server is not None:
    try:
      _server.terminate()
    except Exception:
      pass
    _server = None


def _frame():
  """Runs once per frame, just before the buffer swap."""
  global _remote, _client_state, _server_started
  if not _server_started:
    from client_state import ClientStateReader

    _client_state = ClientStateReader()
    _start_server()
    atexit.register(_shutdown)
    _server_started = True

  connected = _client_state.connected()
  if _remote is None:
    if not connected:
      return
    from remote_ui import RemoteUI

    _remote = RemoteUI()  # window/GL context exists by now

  _remote.update()
  if connected:
    _remote.stream_frame()  # must run before the buffer swap


def _install():
  import pyray as pr

  orig_end_drawing = pr.end_drawing

  def end_drawing():
    global _disabled
    if not _disabled:
      try:
        _frame()
      except Exception as e:
        print(f"remote_ui_auto: disabled ({e})")
        _disabled = True
    orig_end_drawing()

  pr.end_drawing = end_drawing


if not _disabled:
  try:
    _install()
  except Exception as e:
    print(f"remote_ui_auto: failed to install hook ({e})")

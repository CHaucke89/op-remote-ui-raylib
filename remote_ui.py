"""Starts the TouchInjector and FrameStreamer for the openpilot UI."""

import os
import sys

# Flat imports from this directory and ./vendor.
_here = os.path.dirname(os.path.abspath(__file__))
for _p in (_here, os.path.join(_here, "vendor")):
  if os.path.isdir(_p) and _p not in sys.path:
    sys.path.insert(0, _p)

from frame_streamer import FrameStreamer  # noqa: E402
from touch_injector import TouchInjector  # noqa: E402


_ui_state = None


def _engaged() -> bool:
  """Remote input is ignored while engaged."""
  global _ui_state
  if _ui_state is None:
    try:
      # Lazy, so the smoke test runs without openpilot.
      from openpilot.selfdrive.ui.ui_state import ui_state
      _ui_state = ui_state
    except Exception:
      _ui_state = False
  if _ui_state is False:
    return False
  return bool(getattr(_ui_state, "engaged", False))


class RemoteUI:
  def __init__(self):
    self.injector = TouchInjector(input_blocked=_engaged)
    self.injector.start()
    self.streamer = FrameStreamer()
    print("RemoteUI: streaming + input injection started")

  def update(self):
    """Call once per rendered frame, whether or not a browser is connected."""
    self.injector.refresh_blocked()
    self.injector.end_frame()

  def stream_frame(self):
    self.streamer.stream_frame()

  def close(self):
    self.streamer.close()
    self.injector.stop()
    print("RemoteUI: stopped")


if __name__ == "__main__":
  # Smoke test: run stream_server.py separately and open the page.
  import pyray as pr

  injector = TouchInjector()
  injector.start()
  pr.init_window(2160, 1080, "remote_ui smoke test")
  pr.set_target_fps(60)
  streamer = FrameStreamer()
  try:
    x = 0
    while not pr.window_should_close():
      x = (x + 5) % 2160
      pr.begin_drawing()
      pr.clear_background(pr.BLACK)
      pr.draw_rectangle(x, 500, 120, 120, pr.RAYWHITE)
      if pr.is_mouse_button_pressed(0):  # noqa: TID251
        p = pr.get_mouse_position()
        print(f"click at ({p.x:.0f}, {p.y:.0f})")
      injector.end_frame()
      streamer.stream_frame()
      pr.end_drawing()
  finally:
    streamer.close()
    injector.stop()
    pr.close_window()

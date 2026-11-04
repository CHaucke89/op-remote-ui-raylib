"""Captures the raylib framebuffer and publishes JPEG frames to shared memory for stream_server.py.

Only the GPU readback runs on the render thread; encoding happens on a worker thread.
"""

import io
import struct
import threading
import time
from multiprocessing import shared_memory as shm

from PIL import Image
import pyray as pr

from client_state import ClientStateReader

SHM_NAME = "openpilot_ui_frames"  # created at /dev/shm/openpilot_ui_frames
FRAME_DATA_SIZE = 4 * 1920 * 1080  # 8,294,400 bytes (max 1080p RGBA)
METADATA_SIZE = 31
SHM_SIZE = METADATA_SIZE + FRAME_DATA_SIZE

# timestamp, width, height, size, format, ready, source width, source height (pre-scaling)
HEADER_FMT = "<QIIIIBHH2x"

FORMAT_JPEG = 1
JPEG_QUALITY = 85
FRAME_RATE_LIMIT = 10  # FPS

DISCONNECTED_FRAME_RATE_LIMIT = 1
DISCONNECTED_JPEG_QUALITY = 60
MIN_CONNECTED_FRAME_RATE_LIMIT = 4
MAX_CONNECTED_FRAME_RATE_LIMIT = 10
MIN_JPEG_QUALITY = 55
MAX_JPEG_QUALITY = 90
ADAPT_DECAY = 0.75
ADAPT_RECOVER_S = 2.0
ADAPT_HIGH_PRESSURE = 1.1
ADAPT_LOW_PRESSURE = 0.6
JPEG_SIZE_BUDGET_AT_10_FPS = 220_000
# 0.5 is an exact 2x2 reduce and cheaper than a full-size encode; other resizes cost more than they save.
SCALE_STEPS = (0.5, 1.0)
# How far the browser may upscale before it gets the next larger step.
MAX_BROWSER_UPSCALE = 1.33
# Browser queueing delay at which the link counts as congested.
NETWORK_DELAY_BUDGET_MS = 250.0
# How often an unchanged frame is re-announced so the stream doesn't look stalled.
UNCHANGED_KEEPALIVE_S = 1.0


class FrameStreamer:
  def __init__(self):
    self.last_capture_time = 0.0
    self.frame_interval = 1.0 / FRAME_RATE_LIMIT
    self.shm = None
    self.client_state = ClientStateReader()
    self.client_connected = False
    self.target_frame_rate = FRAME_RATE_LIMIT
    self.jpeg_quality = JPEG_QUALITY
    self.encode_pressure_ewma = 0.0
    self.last_adapt_time = 0.0
    self.scale = 1.0
    # Last published frame, used to skip unchanged frames (worker thread only).
    self._last_rgba = None
    self._last_encode_key = None
    self._last_header = None  # header fields after the timestamp
    self._last_publish_mono = 0.0
    self.frames_encoded = 0
    self.frames_unchanged = 0
    # Rate/quality state is shared by the render and worker threads.
    self._state_lock = threading.Lock()
    self._init_shm()

    # One-slot handoff: frames are captured only when the encoder is idle, so they're skipped rather than queued.
    self._pending = None
    self._pending_cv = threading.Condition()
    self._encoding = False
    self._running = True
    self._worker = threading.Thread(target=self._encode_loop, name="FrameStreamerEncoder", daemon=True)
    self._worker.start()

  def _set_frame_rate(self, frame_rate):
    self.target_frame_rate = max(1, frame_rate)
    self.frame_interval = 1.0 / float(self.target_frame_rate)

  def _set_disconnected_mode(self):
    self._set_frame_rate(DISCONNECTED_FRAME_RATE_LIMIT)
    self.jpeg_quality = DISCONNECTED_JPEG_QUALITY
    self.encode_pressure_ewma = 0.0

  def _set_connected_defaults(self):
    self._set_frame_rate(FRAME_RATE_LIMIT)
    self.jpeg_quality = JPEG_QUALITY
    self.encode_pressure_ewma = 0.0

  def _poll_client_state(self):
    connected = self.client_state.connected()
    if connected == self.client_connected:
      return

    self.client_connected = connected
    with self._state_lock:
      if connected:
        self._set_connected_defaults()
      else:
        self._set_disconnected_mode()

  def _pick_scale(self, width):
    requested = self.client_state.requested_width() if self.client_connected else 0
    if requested <= 0:
      return 1.0
    for scale in SCALE_STEPS:
      if width * scale * MAX_BROWSER_UPSCALE >= requested:
        return scale
    return 1.0

  def _jpeg_size_budget(self):
    return int(JPEG_SIZE_BUDGET_AT_10_FPS * (FRAME_RATE_LIMIT / float(self.target_frame_rate)))

  def _update_adaptive_rate(self, now_mono, encode_ms, jpeg_size):
    if not self.client_connected:
      return

    frame_budget_ms = 1000.0 / float(self.target_frame_rate)
    encode_pressure = max(
      encode_ms / max(frame_budget_ms, 1.0),
      jpeg_size / max(float(self._jpeg_size_budget()), 1.0),
      self.client_state.network_delay_ms() / NETWORK_DELAY_BUDGET_MS,
    )
    self.encode_pressure_ewma = (ADAPT_DECAY * self.encode_pressure_ewma) + ((1.0 - ADAPT_DECAY) * encode_pressure)
    pressure = self.encode_pressure_ewma

    if pressure > ADAPT_HIGH_PRESSURE and (now_mono - self.last_adapt_time) >= 0.5:
      if self.jpeg_quality > MIN_JPEG_QUALITY:
        self.jpeg_quality = max(MIN_JPEG_QUALITY, self.jpeg_quality - 5)
      elif self.target_frame_rate > MIN_CONNECTED_FRAME_RATE_LIMIT:
        self._set_frame_rate(self.target_frame_rate - 1)
      self.last_adapt_time = now_mono
    elif pressure < ADAPT_LOW_PRESSURE and (now_mono - self.last_adapt_time) >= ADAPT_RECOVER_S:
      if self.target_frame_rate < MAX_CONNECTED_FRAME_RATE_LIMIT:
        self._set_frame_rate(self.target_frame_rate + 1)
      elif self.jpeg_quality < MAX_JPEG_QUALITY:
        self.jpeg_quality = min(MAX_JPEG_QUALITY, self.jpeg_quality + 2)
      self.last_adapt_time = now_mono

  def _init_shm(self):
    # Unlink any stale segment, then recreate.
    try:
      stale = shm.SharedMemory(name=SHM_NAME)
      stale.close()
      stale.unlink()
    except FileNotFoundError:
      pass
    except Exception as e:
      print(f"FrameStreamer: could not clear stale shm: {e}")

    try:
      self.shm = shm.SharedMemory(name=SHM_NAME, create=True, size=SHM_SIZE)
      # Zero the header so a reader never sees a stale ready flag.
      buf = self.shm.buf
      assert buf is not None
      buf[0:METADATA_SIZE] = b"\x00" * METADATA_SIZE
      print(f"FrameStreamer: shared memory ready ({SHM_SIZE} bytes)")
    except Exception as e:
      print(f"FrameStreamer: failed to init shared memory: {e}")
      self.shm = None

  def stream_frame(self):
    """Capture the framebuffer for the encoder. Call on the render thread before end_drawing."""
    if self.shm is None:
      return

    now_mono = time.monotonic()
    self._poll_client_state()
    if (now_mono - self.last_capture_time) < self.frame_interval:
      return
    with self._pending_cv:
      if self._encoding or self._pending is not None:
        return  # encoder busy, try next frame
    self.last_capture_time = now_mono

    # Flush queued draw commands so the capture is complete.
    flush_batch = getattr(pr, "rl_draw_render_batch_active", None)
    if callable(flush_batch):
      flush_batch()

    # Readback must happen on the GL thread; the encoder takes ownership of the image and frees it.
    rl_image = pr.load_image_from_screen()
    capture_ms = int(time.clock_gettime(time.CLOCK_REALTIME) * 1000)
    with self._pending_cv:
      self._pending = (rl_image, capture_ms, now_mono)
      self._pending_cv.notify()

  def _encode_loop(self):
    while True:
      with self._pending_cv:
        while self._running and self._pending is None:
          self._pending_cv.wait()
        if not self._running:
          return
        job = self._pending
        self._pending = None
        if job is None:
          continue
        self._encoding = True
      try:
        self._encode(*job)
      finally:
        with self._pending_cv:
          self._encoding = False

  def _encode(self, rl_image, capture_ms, capture_mono):
    try:
      encode_start = time.monotonic()
      width = rl_image.width
      height = rl_image.height
      if not width or not height:
        return

      data_size = width * height * 4
      if data_size <= 0:
        return

      # Copy out of raylib's buffer so it can be freed below.
      rgba = bytes(pr.ffi.buffer(rl_image.data, data_size))

      # Static screens repeat for seconds at a time; a byte compare is far cheaper than re-encoding.
      scale = self._pick_scale(width)
      encode_key = (width, height, scale, self.jpeg_quality)
      if rgba == self._last_rgba and encode_key == self._last_encode_key:
        self.frames_unchanged += 1
        if capture_mono - self._last_publish_mono >= UNCHANGED_KEEPALIVE_S:
          self._republish_last(capture_ms, capture_mono)
        return
      self._last_rgba = rgba
      self._last_encode_key = encode_key

      # load_image_from_screen already returns top-to-bottom orientation.
      pil_img = Image.frombuffer("RGBA", (width, height), rgba, "raw", "RGBA", 0, 1)

      pil_img = pil_img.convert("RGB")  # JPEG has no alpha channel
      # Reduce after converting: 3 channels instead of 4.
      self.scale = scale
      if self.scale != 1.0:
        pil_img = pil_img.reduce(round(1 / self.scale))

      with io.BytesIO() as out:
        pil_img.save(out, format="JPEG", quality=self.jpeg_quality)
        jpeg = out.getvalue()
      encode_ms = (time.monotonic() - encode_start) * 1000.0

      if len(jpeg) > FRAME_DATA_SIZE:
        print(f"FrameStreamer: frame too large ({len(jpeg)} bytes), dropping")
        self._last_rgba = None
        return

      shm_buf = self.shm.buf if self.shm is not None else None
      if shm_buf is None:
        return

      # Payload first, header last, so the reader never sees ready=1 over partial data.
      shm_buf[METADATA_SIZE : METADATA_SIZE + len(jpeg)] = jpeg
      self._last_header = (
        pil_img.width,
        pil_img.height,
        len(jpeg),
        FORMAT_JPEG,
        1,  # ready
        width,
        height,
      )
      # capture time (epoch ms), not encode time
      shm_buf[0:METADATA_SIZE] = struct.pack(HEADER_FMT, capture_ms, *self._last_header)
      self._last_publish_mono = capture_mono
      self.frames_encoded += 1
      with self._state_lock:
        self._update_adaptive_rate(capture_mono, encode_ms, len(jpeg))
    except Exception as e:
      print(f"FrameStreamer error: {e}")
    finally:
      # Free the raylib image so a frame isn't leaked per tick.
      pr.unload_image(rl_image)

  def _republish_last(self, capture_ms, capture_mono):
    """Re-announce the JPEG already in shm with a new timestamp."""
    shm_buf = self.shm.buf if self.shm is not None else None
    if shm_buf is None or self._last_header is None:
      return
    shm_buf[0:METADATA_SIZE] = struct.pack(HEADER_FMT, capture_ms, *self._last_header)
    self._last_publish_mono = capture_mono

  def close(self):
    # Stop the encoder before unmapping shm.
    with self._pending_cv:
      self._running = False
      job, self._pending = self._pending, None
      self._pending_cv.notify()
    self._worker.join(timeout=2.0)
    if job is not None:
      pr.unload_image(job[0])

    self.client_state.close()
    if self.shm is not None:
      try:
        self.shm.close()
        self.shm.unlink()
      except Exception:
        pass
      self.shm = None

"""Browser client state (count, network delay, requested width) shared from stream_server to the UI.

A tiny shm segment the UI can read every frame without syscalls. The writer never unlinks it,
since readers' mmaps are bound to the inode.
"""

import mmap
import os
import struct

SHM_PATH = "/dev/shm/openpilot_ui_clients"
STATE_FMT = "<III"  # client count, worst queue delay (ms), requested width (0 = full)
STATE_SIZE = struct.calcsize(STATE_FMT)


class ClientStateWriter:
  """Owned by stream_server."""

  def __init__(self):
    fd = os.open(SHM_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
      if os.fstat(fd).st_size < STATE_SIZE:
        os.ftruncate(fd, STATE_SIZE)
      self._map = mmap.mmap(fd, STATE_SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    finally:
      os.close(fd)
    self.set_client_count(0)
    self.set_network_delay_ms(0)
    self.set_requested_width(0)

  def set_client_count(self, count: int) -> None:
    struct.pack_into("<I", self._map, 0, max(0, count))

  def set_network_delay_ms(self, delay_ms: float) -> None:
    struct.pack_into("<I", self._map, 4, max(0, min(int(delay_ms), 0xFFFFFFFF)))

  def set_requested_width(self, width: int) -> None:
    struct.pack_into("<I", self._map, 8, max(0, min(int(width), 0xFFFFFFFF)))

  def close(self) -> None:
    self.set_client_count(0)
    self.set_network_delay_ms(0)
    self.set_requested_width(0)
    self._map.close()


class ClientStateReader:
  """Used by the UI process. Cheap enough to call every frame."""

  def __init__(self):
    self._map: mmap.mmap | None = None

  def _open(self) -> bool:
    try:
      fd = os.open(SHM_PATH, os.O_RDONLY)
    except FileNotFoundError:
      return False
    try:
      if os.fstat(fd).st_size < STATE_SIZE:
        return False
      self._map = mmap.mmap(fd, STATE_SIZE, mmap.MAP_SHARED, mmap.PROT_READ)
      return True
    finally:
      os.close(fd)

  def connected(self) -> bool:
    # Retry until stream_server creates the segment.
    if self._map is None and not self._open():
      return False
    assert self._map is not None
    return struct.unpack_from("<I", self._map, 0)[0] > 0

  def network_delay_ms(self) -> int:
    """Worst queueing delay reported by connected browsers, 0 if unknown."""
    if self._map is None and not self._open():
      return 0
    assert self._map is not None
    return struct.unpack_from("<I", self._map, 4)[0]

  def requested_width(self) -> int:
    """Widest frame (in pixels) any connected browser wants, 0 for full resolution."""
    if self._map is None and not self._open():
      return 0
    assert self._map is not None
    return struct.unpack_from("<I", self._map, 8)[0]

  def close(self) -> None:
    if self._map is not None:
      self._map.close()
      self._map = None

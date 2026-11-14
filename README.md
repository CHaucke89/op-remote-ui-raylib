# OpenPilot Remote UI Streaming

Real-time browser streaming and remote input for openpilot UI.

This is a port of the original Qt-based remote UI streamer by [ecpp](https://github.com/ecpp/op-remote-ui). The openpilot UI has moved from Qt (C++) to raylib/pyray (Python), so the original implementation is no longer functional in newer releases of openpilot. This fixes that.

Current primary path is Python + raylib/pyray:

- Frame capture: `frame_streamer.py`
- Input injection: `touch_injector.py`
- UI integration helper: `remote_ui.py`
- Browser server: `stream_server.py`

The C++/Qt files are kept in `legacy/` as an implementation reference:

- `legacy/frame_streamer.cc/.h`
- `legacy/touch_injector.cc/.h`
- `legacy/main.cc`

## What This Repository Provides

This project moves openpilot UI frames from the render loop to a browser with no disk writes:

1. Once a browser connects, the UI process grabs the framebuffer each frame (only the GPU readback runs on the render thread) and a worker thread JPEG-encodes it.
2. The encoded frame is written into POSIX shared memory at `/dev/shm/openpilot_ui_frames`.
3. `stream_server.py` reads shared memory and broadcasts frames over one WebSocket per browser.
4. The browser draws frames on a `<canvas>` and sends touch, mouse, scroll and keyboard input back over the same WebSocket (`POST /input` is a fallback).
5. Input is forwarded to `/tmp/ui_touch_socket`, where `touch_injector.py` applies it to pyray input hooks.
6. The server publishes its client count, the browsers' network delay and their requested frame width in `/dev/shm/openpilot_ui_clients`, which the UI uses to adapt.

The browser page can be installed as a PWA ("Add to Home Screen"), which runs it full screen in landscape.

## Current Components

### Python (active)

- `frame_streamer.py`
    - Captures with `pyray.load_image_from_screen()` and hands the image to an encoder thread, so encoding doesn't add to UI frame time. Frames are skipped, not queued, while the encoder is busy.
    - Skips re-encoding when the screen hasn't changed, re-announcing the previous frame once a second so the stream doesn't look stalled.
    - Adapts JPEG quality (55 to 90) and frame rate (4 to 10 FPS) to encode time, frame size and network delay; drops to 1 FPS with no browser connected.
    - Encodes at half resolution when every connected browser is small enough not to need full size.
    - Publishes to shared memory using a packed 31-byte header + payload.

- `touch_injector.py`
    - Listens on Unix socket `/tmp/ui_touch_socket` for newline-delimited JSON input events.
    - Monkeypatches pyray polled input functions (mouse, touch, wheel and keyboard) to expose remote state, falling back to local input.
    - Keyboard events and pasted text are queued and released one frame at a time.

- `remote_ui_protocol.py`
    - Shared IPC layout: shared memory name and header format, and the touch socket path.

- `client_state.py`
    - Tiny shared memory segment the server writes and the UI maps read-only: client count, worst network delay and requested frame width.

- `remote_ui.py`
    - Starts `TouchInjector` and `FrameStreamer`. Remote input is ignored while openpilot is engaged (local touch keeps working).

- `remote_ui_auto.py`
    - Zero-integration hook (see Installation). Starts the server, creates `RemoteUI` when the first browser connects and streams only while one is connected.

- `stream_server.py`
    - Flask + flask-sock server on port `8081`.
    - Reads shared memory via `mmap` and broadcasts the latest frame to connected WebSocket clients.
    - Forwards browser input to the UI and refuses it while openpilot is engaged (the browser shows a banner).
    - Serves the browser client: `remote_ui_client.js`, `remote_ui.css` and the PWA manifest `remote_ui.webmanifest`.

- `screenshot_server.py` (legacy)
    - Older polling server that serves PNG screenshots from `/tmp/ui_frame_*.png`.
    - Kept for compatibility/testing, not the recommended runtime path.

### C++/Qt (legacy)

The C++ files in `legacy/` implement the same shared-memory + socket concepts for the older Qt UI. They are useful as protocol references, but are not the primary integration path for current raylib/pyray UI.

## Shared Memory Contract

Both writer and reader use this packed layout:

```c
struct SharedFrame {
    uint64_t timestamp;   // epoch ms
    uint32_t width;
    uint32_t height;
    uint32_t size;        // JPEG byte length
    uint32_t format;      // 1 = JPEG
    uint8_t ready;
    uint16_t source_width;   // UI size before scaling (input coordinates use it)
    uint16_t source_height;
    uint8_t padding[2];
    uint8_t data[4 * 1920 * 1080];
};
```

- Metadata size: 31 bytes
- Data capacity: 8,294,400 bytes
- Total shared memory size: 8,294,431 bytes

## Installation

On the device (or any machine with an openpilot checkout):

```bash
curl -sL https://raw.githubusercontent.com/CHaucke89/op-remote-ui-raylib/main/install.sh | bash
```

Or from a clone: `./install.sh [OPENPILOT_DIR]` (default `/data/openpilot`).

The installer:

1. Copies the files to `/data/op-remote-ui` (override with `INSTALL_DIR`).
2. Installs Flask, flask-sock and Pillow into `/data/op-remote-ui/vendor` using openpilot's Python (`.venv` if present), so openpilot's own environment is untouched.
3. Adds one marked line to `selfdrive/ui/ui.py` that imports `remote_ui_auto`. It is safe to re-run.

Restart the UI (or reboot) and open `http://DEVICE_IP:8081`.

`remote_ui_auto` wraps `pyray.end_drawing` to capture each frame before the buffer swap, launches `stream_server.py` itself, and starts `RemoteUI` when the first browser connects. No render-loop edits or launch-script changes are needed. Set `OP_REMOTE_UI=0` to disable it without uninstalling.

To remove: `./uninstall.sh [OPENPILOT_DIR]`. openpilot updates may reset `ui.py`; just re-run `install.sh`.

### Manual integration

If you prefer to wire it in yourself, put this directory on `sys.path` and call `RemoteUI` from your render loop:

```python
from remote_ui import RemoteUI

remote = RemoteUI()          # after the window/GL context exists
...
remote.update()              # each frame
remote.stream_frame()        # each frame while a browser is connected, before end_drawing
...
remote.close()
```

Then run `python3 stream_server.py` yourself.

## Endpoints

- `GET /` : Built-in HTML viewer (installable as a PWA)
- `GET /remote_ui.js`, `/remote_ui.css`, `/remote_ui.webmanifest` : Browser client assets
- `WS /stream` : Real-time frame stream, plus input, feedback and view-size messages from the browser
- `POST /input` : Remote input events (JSON), fallback when the WebSocket is down
- `GET /stats` : Cache/client stats
- `GET /stream-sse` : SSE fallback stream

## Input Event Format

Input events are JSON, sent over the WebSocket as `{"type":"input","event":{...}}` or to `POST /input`, and forwarded to `/tmp/ui_touch_socket`.

Examples:

```json
{"type":"click","x":1080,"y":540}
```

```json
{"type":"scroll","x":1080,"y":540,"deltaY":120}
```

```json
{"type":"drag","x":1200,"y":600,"startX":1080,"startY":540}
```

```json
{"type":"keydown","key":"Enter"}
```

```json
{"type":"text","text":"pasted text"}
```

## Smoke Testing

`remote_ui.py` includes a standalone test window in its `__main__` block.

Run it directly from this directory (`python3 remote_ui.py`); imports are flat, so no openpilot package layout is required.

## Troubleshooting

### No frames in browser

1. Confirm shared memory exists: `ls -lh /dev/shm/openpilot_ui_frames`
2. Confirm server is running on port 8081.
3. Check server logs for shared-memory connect/read errors.
4. Confirm `remote.stream_frame()` is being called from the render loop.

### Input does not affect UI

1. Confirm socket exists: `ls -lh /tmp/ui_touch_socket`
2. Confirm `TouchInjector` was started (`RemoteUI()` created successfully).
3. Remote input is ignored while openpilot is engaged; the browser shows a banner when it is.

### Latency is high

1. Check network quality between browser and host.
2. Quality and frame rate adapt automatically; the limits are the `*_JPEG_QUALITY` and `*_FRAME_RATE_LIMIT` constants in `frame_streamer.py`.
3. Reduce competing load on the UI host.

## Notes

- This repository focuses on streaming and input plumbing, not full openpilot deployment orchestration.
- `legacy/main.cc` and the Qt classes document the older integration style and protocol history.

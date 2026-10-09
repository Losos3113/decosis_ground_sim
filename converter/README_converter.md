# Ground Segment - retransmitter + state_holder

Receives a multicast MPEG-2 Transport Stream (STANAG 4609 / MISB ST 0601)
and does two things with it in parallel: forwards the raw stream unchanged,
and decodes video + KLV metadata to produce a snapshot (JPEG + metadata)
for roughly every `INPUT_FPS`-th decoded frame, with the metadata taken
from the exact PTS of that frame (see "Snapshot cadence" and "Protocol:
SNAPSHOT channel" below - this changed from wall-clock ticking in v1/
`DSNAP001` to PTS-based pairing in v2/`DSNAP002`).

Each snapshot's metadata is then enriched with PX4 telemetry pulled from
the ground station simulator (project `decosis_ground_sim`) and the two are
merged into one schema, `DECOSIS-SNAP-2`: KLV carries sensor geometry,
the simulator carries vehicle state (battery, arming, NED velocity) that
KLV has no equivalent for. The result is POSTed to a REST API as
`multipart/form-data` - in v3 this replaced the UDP `DSNAP002` datagram,
removing the ~65507 B size ceiling and the JPEG-quality compromise that
came with it.

## Architecture decription

```
multicast TS input (SRC, SRC_PORT)
    |
    +-- retransmitter.py -- 1:1 forward --> RETRANSMIT_DST:RETRANSMIT_DST_PORT
    |
    +-- state_holder.py  -- demux + ffmpeg decode + KLV parsing
                          --> HTTP POST to SNAPSHOT_API_URL
                             (~1x/s of STREAM time: JPEG paired by PTS with
                              the KLV packet from the SAME access unit)
                              ^
                              |  PX4 telemetry merged in (unified.py)
                              |
                        ground_link.py -- polls GROUND_SIM_URL
                                          /api/<uav>/state once a second
```

Both processes run inside a single container (`entrypoint.sh` starts and
supervises them together). Multicast reception works because both scripts
set `SO_REUSEADDR` - on Linux this means each socket that joins the group
gets its own copy of every incoming datagram, so the two processes never
compete for the same traffic.

## Known issues (found during the v2 rework, left open on purpose)

Four things came up while rebuilding PTS pairing that are deliberately
**not** fixed here - each is detailed in the section named below, this is
just a pointer so they aren't missed:

1. **B-frames would break PTS pairing - still unfixed, but no longer
   silent.** Confirmed with a test (fed a B-frame-encoded stream through
   the production `ffmpeg` command and watched `showinfo`): the decoder
   reorders frames to display order before `select` ever sees them, but
   this code's PTS-tracking table records PTS in decode/feed order. True
   today only if a source ever uses B-frames - the transmitter and camera
   mock in this pipeline both disable them, but a real camera's native
   encoder used via a passthrough mode is unverified. The mispairing
   itself is not fixed, but `state_holder.py` now parses `slice_type` and
   logs a one-time `[VIDEO] WARNING` the moment it sees a B-slice (H.264
   only). See "Unstated assumption: no B-frames (decode order == display
   order)" under **Snapshot cadence**.
2. ~~Tags 21-25 vs. ST 0601.19's Report-on-Change vs. the transmitter's
   plain-omission convention.~~ **Fixed on the `4609` transmitter side**
   (not a converter change - this converter already handled ZLI
   correctly). A different/older sender could still exhibit this; see
   "Tags 21-25 and ZLI" under **Protocol: SNAPSHOT channel**.
3. **`sample.ts` doesn't match a current transmitter's packetization** (SEI
   sent as a separate PES per frame). Handled defensively (see **Snapshot
   cadence**) but the file itself is stale as a byte-for-byte reference.
   See the note under **Testing without a live transmitter**.
4. **KLV/video arrival-order race - design proposed, deliberately deferred,
   not implemented.** `on_frame_ready()` calls `get_snapshot(frame_pts)`
   the instant `ffmpeg` hands back a decoded frame; if the KLV packet for
   that exact PTS hasn't been parsed into `KLVStateStore` yet, the snapshot
   silently carries forward the previous value (correct per Report-on-Change,
   but indistinguishable from "source skipped this tag"). With the `4609`
   transmitter the race doesn't actually occur - video is enqueued before
   KLV in the same loop iteration, so KLV(k) is on the wire microseconds
   after video(k), long before `ffmpeg` decode latency produces the
   matching JPEG - but a different/passthrough source interleaving KLV and
   video differently could hit it. A full fix (pending-frame queue,
   wait-with-timeout, `klv_exact` flag, new wire version `DSNAP003`) is
   written up in `converter/NAVRH_klv_sync.md`, but is intentionally
   **not** implemented: it adds a new thread hand-off, a tunable timeout
   nobody has measured against real hardware, and a breaking wire-format
   change, all to harden a path nothing in this pipeline currently
   exercises - same category of risk as item 1 (B-frames), which got a
   warning instead of a structural fix for the same reason. If an
   out-of-order source is ever actually used, the cheaper first step is a
   one-time `[KLV] WARNING` when a snapshot is sent before its own exact
   PTS's KLV has arrived (no queue, no wire change) - escalate to the full
   `NAVRH_klv_sync.md` design only once that warning actually fires in
   practice.

## Files

`converter/` (build this to run the container):

| File | Purpose |
|---|---|
| `retransmitter.py` | 1:1 UDP relay of the raw TS stream |
| `state_holder.py` | demux, video decode, KLV state tracking, snapshot sender |
| `klv_core.py` | ST 0601 BER-TLV decoding + `KLVStateStore` (single source of truth - imported by both `state_holder.py` and `../testing/ground_klv_state.py`, never copied) |
| `ground_link.py` | background poller of the ground station simulator's REST API; `state_holder.py` only ever reads its in-memory cache, never the network |
| `unified.py` | assembles KLV and PX4 telemetry into one schema (`DECOSIS-SNAP-2`), split into `vehicle_data` / `camera_data` / `mission_data` branches |
| `snapshot_sender.py` | fixed-cadence sender: `Destination` is one independent target (own timeout, lock, counters), `SnapshotSender` encodes the multipart once and fans it out; fire-and-forget, no retry |
| `snapshot_store.py` | writes `.jpg` + `.json` per snapshot, off the ticker thread, with a last-N cap that only ever deletes files it wrote itself |
| `Dockerfile`, `entrypoint.sh`, `docker-compose.yml` | containerization |
| `.env.example` | configuration template |
| `network_preflight.py` | multicast reachability test, independent of everything else (not part of the container image - just a helper script) |

`testing/` (not required to run the container, but solves real problems you will otherwise hit):

| File | Purpose |
|---|---|
| `network_preflight.py` | same tool, duplicated here for convenience |
| `replay.py` | play a `.ts` file back over multicast, for local testing without a live transmitter |
| `mock_api.py` | stand-in REST receiver for the snapshot channel - prints each arriving frame and optionally writes it out as `.jpg` + `.json`. Frames arrive about twice a second, so `--keep N` (default 50) retains only the last N pairs and deletes its own older ones; `--keep 0` disables that. Use this for local testing instead of the three UDP tools below |
| `reference_client.py` | **obsolete** - UDP receiver of the old `DSNAP002` channel, which no longer exists |
| `inspect_json.py` | **obsolete** - same reason |
| `inspect_image.py` | **obsolete** - same reason |
| `ground_klv_state.py` | optional standalone CLI tool for watching the KLV stream directly; imports `klv_core.py` from `../converter/` via a relative `sys.path` entry, so it runs standalone without needing a local copy |

A sample capture (`sample.ts`) is included alongside these folders - see
"Testing without a live transmitter" below.

No `requirements.txt` - everything here uses only the Python standard
library. The one external dependency, `ffmpeg`, is installed via `apt` in
the `Dockerfile` itself, not via pip.

## Running

```
cp .env.example .env
nano .env
docker compose up --build
```

## Why `network_mode: host`

Multicast group membership requires sharing the kernel's network stack
directly. On the default Docker bridge network this typically produces no
error at all - it just silently receives nothing. `network_mode: host`
works reliably on native Linux. It does **not** work reliably on Docker
Desktop for Windows, even with WSL2 mirrored networking enabled - this was
confirmed in practice (Windows itself sees the multicast traffic in
Wireshark, but the WSL2 VM underneath the container does not). If you ever
need to run this on Windows, a separate multicast-to-unicast bridge process
running natively on Windows is required; ask before assuming
`network_mode: host` will work there.

## Multi-NIC hosts: the `IFACE` variable

On a host with more than one network interface, the default route may not
point at the interface the multicast traffic actually arrives on (common
setup: an Ethernet NIC on the STANAG network plus a WiFi NIC with a lower
route metric used as the internet default gateway). When this happens, the
multicast group join silently attaches to the wrong interface and nothing
is ever received - no error, no log line, just permanent silence.

**Diagnose this before touching the container**, with `network_preflight.py`,
which has the exact same `--iface` option:

```
python3 network_preflight.py listen --group 239.1.1.1 --port 5000
```

If this sees nothing, check `ip route` and `ip addr` for the correct
interface IP, then set it as `IFACE` in `.env`. `replay.py` also needs its
own `--iface` when sending on a multi-NIC host, for the same reason -
multicast loopback delivery on a single machine is interface-specific, so
a sender using the wrong interface won't reach a receiver joined on a
different one, even though both processes run on the same physical machine.

## UDP receive buffer

`state_holder.py` asks for an 8 MB socket receive buffer
(`--recv-buffer-mb`) and logs what it actually got. The OS default is often
only tens to hundreds of kB; at higher camera bitrates a short stall in the
Python loop is then enough for the kernel to drop datagrams, which shows up
downstream as TS sync losses and continuity counter gaps. Linux silently
caps the request at `net.core.rmem_max` (commonly ~208 kB), and the log then
says `WARNING: system buffer orizl`. The limit is a host setting, not a
container one (the container runs with `network_mode: host`), so raise it on
the host:

```
sudo sysctl -w net.core.rmem_max=8388608
```

(add `net.core.rmem_max=8388608` to `/etc/sysctl.conf` to keep it after a
reboot).

## ENV variables

| Variable | Default | Used by |
|---|---|---|
| `SRC` / `SRC_PORT` | `239.1.1.1` / `5000` | both |
| `IFACE` | *(empty = let the kernel choose)* | both |
| `RETRANSMIT_DST(_PORT)` | *(required)* | `retransmitter.py` |
| `SNAPSHOT_API_URL` | *(required)* | `state_holder.py` - REST endpoint the snapshots are POSTed to |
| `SNAPSHOT_API_TIMEOUT_MS` | `2000` | `state_holder.py` - one attempt, then the frame is dropped |
| `SNAPSHOT_API_TOKEN` | *(empty = no auth header)* | `state_holder.py` - sent as `Authorization: Bearer ...` |
| `TARGET_API_URL` | *(empty = second destination unused)* | `state_holder.py` - the partner's REST endpoint, see "Three independent consumers" |
| `TARGET_API_TIMEOUT_MS` | `2000` | `state_holder.py` - independent of the primary timeout |
| `TARGET_API_TOKEN` | *(empty = no auth header)* | `state_holder.py` |
| `SNAPSHOT_SAVE_DIR` | *(empty = nothing written)* | `state_holder.py` - where `.jpg` + `.json` go per snapshot |
| `SNAPSHOT_SAVE_KEEP` | `500` | `state_holder.py` - keep only the last N snapshots; `0` removes the cap (~290 MB/hour, mind the disk) |
| `GROUND_SIM_URL` | *(empty = no enrichment)* | `state_holder.py` - base URL of the ground station simulator, e.g. `http://127.0.0.1:8002`. Left empty, snapshots still go out, just with KLV only |
| `GROUND_SIM_UAV` | `uav1` | `state_holder.py` - which UAV in the simulator this stream belongs to |
| `GROUND_SIM_POLL_S` | `1.0` | `state_holder.py` - the simulator advances its CSV once a second, so polling faster buys nothing |
| `GROUND_SIM_TIMEOUT_MS` | `1000` | `state_holder.py` |
| `GROUND_SIM_MAX_AGE_S` | `10` | `state_holder.py` - telemetry older than this is dropped rather than attached to a frame |
| `INPUT_FPS` | *(empty = auto-detect)* | `state_holder.py` - empty: measured from the stream's own PTS spacing between picture access units (`FrameRateDetector`). Set only to override the measured value |
| `JPEG_QUALITY` | `2` | `state_holder.py` - ffmpeg `-q:v` (lower = better quality, larger file). No longer constrained by datagram size - HTTP has no 65507 B ceiling - so pick it for what has to be visible in the image |
| `SEND_INTERVAL_S` | `1.0` | `state_holder.py` - one message per this many seconds **to each destination**. It is also the receiver's timeout: silence longer than this means something is wrong |
| `SNAPSHOT_SAVE_DIR_HOST` | `./snapshots` | `docker-compose.yml` - host folder bound to `SNAPSHOT_SAVE_DIR` inside the container; must be writable by uid 1000 |
| `REPLAY_RATE` | `6M` | `replay` service - playback bitrate; faster than the file's real rate speeds the whole conversion up |
| `REPLAY_TTL` | `8` | `replay` service - multicast TTL, i.e. how many routers the stream may cross. `1` keeps it on this machine |
| `REPLAY_VIDEO_DIR` | `./video` | `replay` service - host folder holding the `.ts` to play |
| `RECV_BUFFER_MB` | `8` | `state_holder.py` - UDP receive buffer; raise it when the log reports packet loss |
| `PTS_HORIZON_S` | `30` | `state_holder.py` - how long a PTS waits for its frame out of ffmpeg before being given up as untraceable |

Everything configurable lives in `.env` - there is nothing to edit in
`docker-compose.yml` or `entrypoint.sh` for normal operation.

**One gotcha:** `entrypoint.sh` is copied into the image, so a change to it
only takes effect after `docker compose up -d --build`. Changes to `.env`
alone need no rebuild, just `docker compose up -d`.

## Snapshot cadence

**v1 → v2 change.** v1 (`DSNAP001`) ran a 1 Hz wall-clock ticker that
resent the latest decoded frame together with whatever metadata happened
to be current at tick time - up to ~1 s of drift between the two, worse on
network jitter, and a duplicate frame re-sent with fresher metadata on a
slow tick. v2 (`DSNAP002`) removes the ticker: a snapshot is sent exactly
when `ffmpeg` produces a new decoded frame, and its metadata is looked up
by that frame's own PTS (`KLVStateStore.get_snapshot(frame_pts)` in
`klv_core.py`) - the same PTS the KLV packet for that exact access unit
carried in its PES header (MISB ST 1402 synchronous metadata). No frame is
ever resent with different metadata, and no snapshot mixes data from two
different moments.

**v3: fixed send cadence.** Sending is decoupled from decoding again, but
not the v1 way. The decoder hands each selected frame to
`SnapshotSender.submit()`, which keeps only the newest unsent one. On a
fixed wall-clock grid (`--interval-s`, default 1 s) exactly one message
goes out: that newest frame, with metadata built for its own PTS - or a
heartbeat when there is no new frame, it has no KLV, or the previous POST
is still in flight. No frame is sent twice and no frame carries another
frame's metadata, so v1's problems don't come back; the cost is up to one
interval of extra latency. Measured gaps between consecutive messages:
0.97-1.02 s, whether frames arrive faster (fast replay), not at all, or
the API is slow. Frames replaced by a newer one within the same interval
are counted as `prorezano` in the final summary and do not consume a
`frame_number`.

`ffmpeg` downsamples to every K-th decoded picture via
`-vf select=not(mod(n\,K))` - chosen deliberately over
`-vf fps=1` because `fps=1` selects frames using PTS values `ffmpeg` itself
fabricates for a raw elementary stream (from `INPUT_FPS` alone, since raw
H.264/HEVC carries no real timing), a heuristic that can differ across
`ffmpeg` versions; `select=not(mod(n\,K))` instead picks strictly by input
frame COUNT (every Kth), a simple, version-stable behavior confirmed
against `ffmpeg` 8.0.1 (see `state_holder.py`'s `VideoFrameDecoder`
docstring for the exact test performed). `state_holder.py` tracks, on its
own, the real PTS of every access unit it hands to `ffmpeg`, keyed by a
counter it maintains itself, and reads back the matching PTS when the
corresponding JPEG comes out the other end - `ffmpeg`'s own internal
timestamps are never trusted for this. That counter only advances on
access units that actually contain a VCL/slice NAL (a real picture) -
confirmed necessary against `sample.ts`, where some access units are
SEI-only and produce no `ffmpeg` output at all; counting them would have
desynchronized the pairing from the second frame onward. An access unit
outside this window's reach (e.g. because `--input-fps` doesn't match the
real encoder output rate) is dropped rather than sent unpaired, with a
`[VIDEO] WARNING` in the log.

K is half the number of *decoded pictures per second* (times the send
interval), so the decoder produces about two frames per send slot - with
exactly one per slot, a small phase shift against the send grid would
leave some slots without a fresh frame (heartbeat instead) and the next
one with a surplus. A wrong picture rate therefore no longer changes how
often messages go out, only how many frames are decoded per slot. The
rate itself is not taken on trust: `FrameRateDetector` buffers the first
access units, takes the median PTS spacing between the ones that carry a
picture, and derives the rate from that before `ffmpeg` is started (the
buffered units, including SPS/PPS/SEI-only ones, are then fed in their
original order). Measuring on picture access units rather than all of
them matters: on `sample.ts`, where every other access unit is SEI-only,
PTS ticks every 1/30 s but only 15 pictures/s are decoded. `--input-fps` /
`INPUT_FPS` still overrides the measurement. The PTS-lookup window is
`--pts-horizon-s` (default 30 s) worth of access units.

Nothing is sent to `ffmpeg` until the first access unit carrying an SPS
(H.264 NAL 7 / HEVC NAL 33). Joining a live stream - or restarting the
converter - almost always lands mid-GOP; everything before the next SPS
fails to decode ("non-existing PPS"), and if that run is long enough
`ffmpeg` exits outright ("Decode error rate ... exceeds maximum"). Each such
undecodable picture would also advance the converter's own frame counter
but not `ffmpeg`'s `n`, shifting every later PTS pairing. `sample.ts`
itself starts mid-GOP: its first 55 access units (27 pictures) come before
the first SPS and are now dropped, logged once as `[VIDEO] SPS nalezeno -
... zahozeno N access units`.

### Unstated assumption: no B-frames (decode order == display order)

The PTS-tracking table (`_pts_by_au_index`) records each access unit's PTS
in the order `feed()` receives it - i.e. DECODE order, since that is the
order access units arrive on the wire. `ffmpeg`'s decoder, however, always
hands frames to its filter chain (where `select` counts them) in DISPLAY
order - confirmed by feeding a stream encoded with B-frames through the
exact production command and watching `showinfo`: frame *types* came out
interleaved (I, B, B, B, P, B, B, B, P, P, I, ...) while `pts_time` stayed
perfectly monotonic, proving the decoder had already reordered before the
filter chain saw anything. **Decode order and display order are identical
only when the source has no B-frames** - true for a transmitter whose
encoder sets `bframes=0`, and true for a camera mock that sets `-bf 0`
with the comment "no B-frames: decode order = display order" - but **not
guaranteed for every real camera's native encoder** feeding this pipeline
through a passthrough mode that proxies a source's stream without
normalizing its GOP structure. If a source with B-frames is ever used,
this state_holder would pair each frame with the WRONG PTS. The
mispairing itself is **not fixed** - that would mean either tracking
decode vs. display order properly or rejecting B-frame sources outright,
both bigger changes than today's scope - but it is no longer **silent**:
`feed()` parses `slice_type` out of each H.264 slice's exp-golomb-coded
header (`_h264_contains_b_slice()`/`_h264_slice_is_b()`) and logs a
one-time `[VIDEO] WARNING: B-frame detected...` the moment it sees one,
instead of just mispairing quietly. Tested against a real `bframes=3`
stream (fires exactly once) and a real `bframes=0` stream (never fires -
no false positives) through the actual `ffmpeg` pipeline. HEVC is not
covered - its slice header needs PPS context to parse `slice_type`, out
of scope for a one-time diagnostic.

## Behavior when one process crashes

`entrypoint.sh` watches both processes with `wait -n`. If either one dies,
the entrypoint stops the other one too and exits with a non-zero code, so
Docker's `restart: unless-stopped` restarts the whole container - both
processes together, rather than leaving one silently dead while the other
keeps running with a half-working result.

This only helps if `state_holder.py` itself actually dies when something
inside it goes wrong, which it previously did not always do: a crash in
its internal `ffmpeg` subprocess, or an exception on its snapshot-sending
thread, used to leave the Python process running (silently sending nothing
new, or resending a stale frame with fresh metadata) with nothing to
trigger `wait -n`. `state_holder.py`'s main loop now polls, at least once a
second, whether `ffmpeg` has exited (`VideoFrameDecoder.has_exited()`) or
its frame-reading thread has died (`reader_alive()`), and exits non-zero
itself the moment either happens - so a decoder crash reaches
`entrypoint.sh`/Docker instead of stopping short of it.

A single malformed KLV packet used to be able to cause this kind of crash
too: a truncated or corrupted tag/length field could walk past the end of
the buffer inside `parse_klv_packet()` and raise an uncaught `IndexError`,
taking down the whole process over one bad datagram. `parse_klv_packet()`
now catches that and returns `None` (treated the same as any other
unparseable packet) instead of propagating it - confirmed with truncated,
oversized-length-claim, and garbage KLV payloads, all handled without a
crash, with a normal well-formed packet still decoding correctly right
after.

## Protocol: RETRANSMIT channel

Raw MPEG-2 Transport Stream, forwarded byte-for-byte. Every UDP datagram
is exactly what arrived from the multicast source. Any standard MPEG-TS
demuxer handles it without modification.

## Protocol: SNAPSHOT channel

One HTTP POST = one frame + the metadata that applied to that EXACT
frame's PTS (see "Snapshot cadence" above), merged with the PX4 telemetry
read from the ground station simulator.

```
POST <SNAPSHOT_API_URL>
Content-Type: multipart/form-data; boundary=...

  part "metadata"   Content-Type: application/json; charset=utf-8
    {"schema": {"name": "DECOSIS-SNAP-2", "kind": "snapshot",
                "frame_number": 123, ...}, "vehicle_data": {...}, ...}

  part "image"      Content-Type: image/jpeg, filename="frame_123.jpg"
    <raw JPEG bytes, SOI 0xFFD8 ... EOI 0xFFD9>
```

Both parts ride in one request body, so the atomicity the UDP datagram
used to give for free is preserved: what arrives together belongs
together, same PTS, same access unit. Splitting them into two requests
would allow them to drift or interleave, which is why it is not done.

Multipart rather than JSON with a base64 image: base64 would inflate the
payload by ~33 % and reintroduce exactly the "compress harder because of
transport size" pressure that moving off UDP was meant to remove.

`Authorization: Bearer <SNAPSHOT_API_TOKEN>` is added when that variable is
set, and omitted entirely when it is empty.

**Error strategy: drop, never retry.** One failed POST means that one
frame is lost, nothing more - the same philosophy as the old
`except OSError` around `sock.sendto()`, just a different transport. No
queue, no retry, no spooling to disk. The POST runs on a short-lived
background thread, because a synchronous one would block the decoder's
read loop for the duration of the timeout and stall decoding itself, not
just sending. If a POST is still in flight when the next frame is ready,
that frame is dropped rather than queued - the same "drop, don't retry"
rule extended to congestion. Both counts appear in the final summary line.

### Heartbeat

In a send slot (see "v3: fixed send cadence") that has no frame to send,
an empty envelope goes to the same URL: the same multipart request with
only the `metadata` part and no `image` part.

```
  part "metadata"   Content-Type: application/json; charset=utf-8
    {"schema": {"name": "DECOSIS-SNAP-2", "kind": "heartbeat",
                "sent_at": "2026-10-08T18:21:38.182Z"}}
```

Every message carries `schema.kind`: `"snapshot"` for a frame (with
`image`), `"heartbeat"` for the empty envelope (without `image`) - a
receiver branches on that one field, in the same place for both. Exactly one message goes out per
`--interval-s`, so a receiver can treat "nothing for ~3 s" as the
converter being down. Heartbeats use their own in-flight lock - a slow
snapshot POST cannot delay one - and follow the same drop-don't-retry
rule.

### Three independent consumers

Every snapshot goes to three places, and **none of them can hold up the
others**:

| Consumer | Config | On failure |
|---|---|---|
| `primary` API | `SNAPSHOT_API_URL` | logged once, frame dropped for that destination |
| `partner` API | `TARGET_API_URL` | same, independently |
| disk | `SNAPSHOT_SAVE_DIR` | logged once, frame not written; sending unaffected |

The isolation is the point, not an optimisation. **The partner API is
written by the partner, not by us** - it may be slow, hang until its
timeout, or answer with something unexpected, and none of that may slow our
own channel or stall decoding. So each destination gets its own timeout,
its own in-flight lock and its own counters, and logs under its own name
(`[SEND:target]`, `[SEND:primary]`, `[STORE]`) - when something breaks at
3am, the log already says whose side it is.

Each destination still receives **exactly one message per `--interval-s`**:
the snapshot if it is free, a heartbeat if its previous POST is still in
flight. The "nothing for ~3 s means the converter is down" guarantee
therefore holds per destination, not just globally. The multipart body is
encoded once and handed to every destination.

Disk writes also run off the ticker thread - a slow or full disk would
otherwise delay sending, which is exactly the coupling this project unpicks
elsewhere. A snapshot is written **even when every POST fails**: the file is
the durable record. `.json` is written before `.jpg`, so a run that dies
mid-write leaves metadata without an image (which is detectable) rather
than an image nobody can place.

**Capacity is a real constraint.** At one message per second and ~75 kB per
JPEG the folder grows by roughly 290 MB per hour - about 7 GB a day.
`SNAPSHOT_SAVE_KEEP` (default 500) keeps only the last N snapshots and
deletes older ones; `0` disables the cap and leaves housekeeping to the
operator. Rotation only ever deletes files **this run wrote** - it never
scans the folder, so anything else living there is safe.

The container writes as uid 1000 (`grounduser`). `docker-compose.yml` binds
`./snapshots` to `/data/snapshots`; that host folder must be writable by
that uid, which is why it is committed (empty, with a `.gitkeep`) rather
than left for Docker to create as root. If the folder turns out to be
unwritable, the converter logs a warning and runs on **without** saving
rather than refusing to start.

### Frames without KLV are dropped

A decoded frame whose metadata snapshot carries **no KLV tag at all** is not
POSTed - it is counted and discarded. Only frames with sensor data go out.
Ground-sim telemetry does not substitute for this test: it describes the
aircraft, not what the camera was looking at, so a frame without KLV would
arrive with no sensor geometry, which is of no use to a consumer.

`frame_number` still increments for a dropped frame, so a gap in the
numbering tells the consumer that something was discarded rather than
silently renumbering around it. The total appears in the closing summary as
`dropped (no KLV)`, and the first occurrence logs a one-off warning.

This is the branch point where future handling of KLV-less frames belongs
(a separate queue, endpoint, or schema) - see the comment on
`on_frame_ready` in `state_holder.py`.

### KLV carriage: two forms in the wild

STANAG 4609 streams carry KLV in either of two ways, both legal per MISB
ST 1402, and this converter accepts both:

| `stream_type` | How it looks | Where the KLV starts in the PES payload |
|---|---|---|
| `0x15` | metadata in PES (SMPTE 336M) | after a 5-byte metadata AU cell header |
| `0x06` | private PES + registration descriptor `KLVA` | at the first byte |

Rather than trusting the PMT, `_klv_from_pes_payload` locates the UAS Local
Set universal key itself - a recording from a third-party transmitter can
disagree with its own PMT, while that key is unambiguous. If the key is not
found, a one-off warning says so instead of letting "no metadata" pass as a
normal, silent state.

**Known limitation - KLV without PTS.** Some real recordings carry the KLV
PES with no PTS at all (`PTS_DTS_flags == 00`), even though every video PES
has one. `KLVStateStore.update()` drops such packets, because there is
nothing to place them on the stream timeline with; combined with the rule
above, every frame from such a stream is then dropped. Making that data
usable requires deciding how a PTS-less KLV packet should be attributed to a
frame - a change to pairing semantics, deliberately not picked here.

### Wire format history

| Version | Transport | What changed |
|---|---|---|
| `DSNAP001` (v1) | UDP datagram, binary header | metadata paired with the frame by a wall-clock ticker |
| `DSNAP002` (v2) | UDP datagram, binary header | identical byte layout, but metadata paired by PTS - `age_s` changed meaning, which is why the magic changed rather than staying `DSNAP001` |
| `DECOSIS-SNAP-1` (v3) | HTTP POST, multipart | no binary header at all; the UDP size ceiling is gone. KLV and ground-sim telemetry merged into one set of semantic sections, KLV taking precedence, disagreements listed in `conflicts` |
| `DECOSIS-SNAP-2` (v4, current) | HTTP POST, multipart | same transport. The merge is dropped: data is split by origin into `vehicle_data` / `camera_data` / `mission_data`, so a quantity both sources report now appears in both branches and the consumer chooses. `schema` became an object carrying kind, frame number and UAV identity; `conflicts` is gone |

v3 drops the magic-header scheme entirely: the version now travels as the
`schema` field inside the JSON, where a consumer reads it without
byte-offset parsing. The old UDP tools (`reference_client.py`,
`inspect_json.py`, `inspect_image.py`) no longer apply - use
`testing/mock_api.py`, which receives the HTTP channel and can write each
frame out as `.jpg` + `.json`.

### JSON metadata structure

Schema `DECOSIS-SNAP-2`, built by `unified.py`. Four top-level branches,
split by **where the data came from**:

```json
{
  "schema":       { ... },   // what this message is: version, kind, frame, UAV, sources
  "vehicle_data": { ... },   // everything from the ground station simulator (PX4)
  "camera_data":  { ... },   // everything from KLV in the video stream (ST 0601)
  "mission_data": { ... }    // mission identity
}
```

A branch with nothing to put in it is **absent from the message** - not
empty, not `null`. Read defensively (`dict.get`, never `dict[...]`).

#### The `schema` block

Describes the message rather than carrying measurements, so its values are
plain, not the field objects used everywhere else.

```json
"schema": {
  "name": "DECOSIS-SNAP-2",
  "kind": "snapshot",                      // or "heartbeat" - see "Heartbeat"
  "frame_number": 97,
  "pts": 1350268184,
  "uav": { "id": "uav1", "designation": "ScanEagle" },
  "sources": {
    "camera":  { "present": true, "tag_count": 20, "timing": "pes_pts" },
    "vehicle": { "present": true, "uav_id": "uav1", "age_s": 0.72,
                 "topics": ["battery_status", "..."] }
  }
}
```

`schema.kind` is the **only** thing a consumer should branch on to tell a
snapshot from a heartbeat - not the presence of the image part.

`uav.id` comes from the simulator, `uav.designation` from KLV tag 10; either
key is omitted when unknown.

#### Every data field is an object

Inside `vehicle_data`, `camera_data` and `mission_data`, no value stands
alone - each carries where it came from and how old it is:

```json
"platform_latitude": {
  "value":   50.264279665548486,
  "source":  "klv:13",                 // klv:<tag> or ground_sim:<topic>
  "age_s":   0.0,
  "age_ref": "frame_pts_derived"       // what that age is measured against
}
```

`age_ref` is not decoration. The two sources have no common clock, so their
ages mean different things and the numbers are not comparable without it:

| `age_ref` | Source | Meaning |
|---|---|---|
| `frame_pts` | KLV | Stream time (PTS) between this frame and the KLV packet that last reported the value. `0.0` means the same access unit. Exact pairing. |
| `frame_pts_derived` | KLV | The same, but this stream's KLV PES carried no PTS, so the demuxer assigned the last video PES timestamp. Accurate to roughly a frame. |
| `fetch_wallclock` | ground sim | Wall-clock seconds since the telemetry was pulled from the simulator's REST API. **No relationship to PTS whatsoever** - the simulator ticks on its own clock. |

Conflating those two under one unlabelled key is the mistake that forced
the v1 -> v2 magic change, which is why the label sits on every field.

#### The same quantity can appear twice

The split is by origin, so a value that both sources report lands in both
branches - aircraft position is in `vehicle_data` (from PX4) **and** in
`camera_data` as `platform_latitude` / `platform_longitude` (from KLV tags
13/14, which describe the aircraft even though they arrive over the video
stream). Likewise heading, pitch and roll.

Both are correct, each from its own source; **which one applies is the
consumer's decision.** This is deliberate (decided during the v2 design).
`DECOSIS-SNAP-1` instead merged them into one value with KLV taking
precedence and recorded disagreements in a `conflicts` array - that merge,
and `conflicts` with it, are gone.

#### What goes where

| Branch | Source | Contents |
|---|---|---|
| `vehicle_data` | ground sim | Position, altitude, attitude from the quaternion, NED position and velocity, `armed` / `nav_state` / `failsafe`, battery voltage, current and remaining. |
| `camera_data` | KLV | Capture time, sensor geometry (FOV, relative azimuth/elevation/roll, slant range, target width), frame centre, **and** aircraft position and attitude as reported by KLV. |
| `mission_data` | KLV | Mission ID, security local set, MIIS core identifier. Absent whenever the stream reports none of them. |

Raw source payloads are **not** sent. `DECOSIS-SNAP-1` and the first draft of
v2 carried a `raw` branch holding both sources verbatim; dropping it cuts the
message by roughly 40 % and costs almost nothing, because every KLV tag that
`KLVStateStore` emits has a mapped field above. The one casualty is the
simulator's `timesync_status` topic, which has no mapping and is therefore
no longer forwarded.

### Tags 21-25 and ZLI

Per the Report-on-Change rule above, this converter treats ANY tag
missing from a packet as "unchanged, carry the last value forward"
(bounded to 30 s) - **not** as "invalid now", because that is what MISB
ST 0601.19 actually specifies for a plain omission. A transmitter that
omits Tags 21/22 (Slant Range/Target Width) or 23-25 (Frame Center)
outright on an invalid measurement, instead of sending them as a
Zero-Length Item, is relying on that 30 s grace period rather than
immediate invalidation - this converter (a generic, spec-following ST
0601 receiver) already handles ZLI correctly if the sender uses it, no
change needed on this side either way.

The known transmitter this was handed off from (the `4609` project) used
to omit rather than ZLI these tags; that has since been fixed there
(`build_st0601_packet()` now sends ZLI), so geometry now disappears
immediately instead of lingering for 30 s. If you're feeding this
converter from a different or older transmitter and see stale Frame
Center/Slant Range values hang around after they should be invalid,
that's this same convention - the fix belongs on the sending side (send
ZLI instead of omitting), not here.

### KLV tags

| Tag | Name | Notes |
|---|---|---|
| 2 | Precision Time Stamp | microseconds since epoch |
| 3 | Mission ID | static |
| 5 | Platform Heading | degrees, 0-360 |
| 6 | Platform Pitch | degrees |
| 7 | Platform Roll | degrees |
| 10 | Platform Designation | static |
| 11 | Image Source Sensor | static |
| 12 | Image Coordinate System | static |
| 13 | Sensor Latitude | degrees |
| 14 | Sensor Longitude | degrees |
| 16 | Sensor HFOV | degrees |
| 17 | Sensor VFOV | degrees |
| 18 | Sensor Relative Azimuth | degrees |
| 19 | Sensor Relative Elevation | degrees |
| 20 | Sensor Relative Roll | degrees |
| 21 | Slant Range | meters, absent if the source has no valid measurement |
| 22 | Target Width | meters, absent if the source has no valid measurement |
| 23 | Frame Center Latitude | absent if the source has no valid frame center |
| 24 | Frame Center Longitude | absent if the source has no valid frame center |
| 25 | Frame Center Elevation (MSL) | absent if the source has no valid frame center |
| 48 | Security Local Set | static, nested object (`classification`, `classifying_country`, ...) |
| 65 | UAS LS Version | static |
| 75 | Sensor Ellipsoid Height (HAE) | meters |
| 94 | MIIS Core Identifier | static, nested object (`sensor_uuid`, `platform_uuid`, ...) |

## Testing without a live transmitter

A sample capture (`sample.ts`) is included. Play it back, as many times as
needed:

```
python3 testing/replay.py --file sample.ts --dst 239.1.1.1 --port 5000 --loop
```

Add `--iface` if the host has multiple NICs (see above).

### Full local loop

Three terminals reproduce the whole chain on one machine, no real API and
no live transmitter involved:

```
# 1. stand-in for the REST API - prints what arrives, writes .jpg + .json
#    --keep caps the folder at the last N frames (default 50); --keep 0 for
#    unlimited, or drop --save-dir entirely to only print and write nothing
python3 testing/mock_api.py --port 9000 --save-dir /tmp/snapshots --keep 50

# 2. the ground station simulator (other repo), serving PX4 telemetry
cd ../decosis_ground_sim && docker compose up

# 3. the converter itself, pointed at both
python3 converter/state_holder.py \
    --src 127.0.0.1 --src-port 5000 \
    --api-url http://127.0.0.1:9000/snapshot \
    --ground-sim-url http://127.0.0.1:8002 --ground-sim-uav uav1

# then, in a fourth: feed it the sample capture
python3 testing/replay.py --file sample.ts --dst 127.0.0.1 --port 5000
```

`--src 127.0.0.1` is not a multicast address, so `state_holder.py` falls
back to plain unicast listening - handy for a loopback-only test.

Worth testing on purpose, since both are designed to degrade rather than
fail: stop the simulator and the frames keep flowing with KLV only
(`sources.ground_sim.present` goes `false` once the last sample passes
`GROUND_SIM_MAX_AGE_S`); stop `mock_api.py` and each frame is dropped with
a single warning, with no retry and no backlog when it returns.

### Simulating the two APIs

Neither destination has to exist for the converter to be exercised. Two
compose services stand in for them, behind the `mock` profile:

```
docker compose --profile mock --profile replay up -d
docker compose logs -f mock-target-api
```

That is the whole loop on one machine - looping test video in, both
receivers answering, one line per frame:

```
mock-target-api-1  | [13:21:03] #240: JPEG 82960 B, metadata 5083 B,
                        20 KLV tagu, vehicle 5 topicu (0.23 s) | poli: 19 vehicle + 19 camera
```

| Profile | Starts |
|---|---|
| *(none)* | converter only - real operation, real transmitter, real APIs |
| `mock` | + stand-in receivers on `MOCK_API_PORT` / `MOCK_TARGET_API_PORT` |
| `replay` | + looping test video from `converter/video/` |

`MOCK_API_PORT` and `MOCK_TARGET_API_PORT` **must agree with the ports in
`SNAPSHOT_API_URL` and `TARGET_API_URL`** - compose cannot check that for
you, since each side only knows its own half.

For debugging the message itself rather than the flow, `print_server.py` at
the repository root dumps the entire structure of each request instead of
one summary line:

```
python3 print_server.py --port 9100
```

Point `TARGET_API_URL` at it and every field of every message is printed
as it arrives - useful when the partner reports that something in the
payload is not what they expected.

### The `replay` service: swapping the video

For demos and testing, a compose service plays a `.ts` file from
`converter/video/` into the multicast group, on loop. Swapping the video is
therefore just:

```
docker compose --profile replay down
# drop a different .ts into converter/video/ and delete the old one
docker compose --profile replay up -d
```

No config edit: the service plays whatever single `.ts` it finds in that
folder (`replay.py --dir`). With more than one file it plays the
alphabetically first and **says so in the log** - otherwise someone swaps
the video, forgets to delete the old one, and wonders why nothing changed.

**It sits behind a compose profile on purpose.** A plain `docker compose up`
does not start it, because in real operation the aircraft's own transmitter
feeds that same multicast group - two sources on one group would interleave
their packets and leave the stream unusable.

| Command | What runs |
|---|---|
| `docker compose up -d` | converter only - real operation |
| `docker compose --profile replay up -d` | converter + looping test video |

The service needs no build: `replay.py` is pure standard library, so it is
mounted read-only into a stock `python:3.12-slim`. `REPLAY_RATE` (default
`6M`) sets the playback bitrate - sending faster than the file's real
bitrate speeds up the whole conversion, which is handy when you want many
frames quickly.

### The reference test recording

`DECOSIS VIDEA/VIDEA_TS/20221005_071612Z_XXXX_0000045 - clip 2026-06-02 07-40-13.694.ts`

Of the four real recordings available, this is the one to test against.
Measured properties of all four:

| | **20221005 (chosen)** | 20220930 | 20230614 | day_flight_fixed |
|---|---|---|---|---|
| length / size | **162 s, 62 MB** | 237 s, 90 MB | 162 s | 194 s, 98 MB |
| H.264 decode errors | 68, **all at the clip start** | 0 | errors | 0 |
| 188-byte alignment | **clean** | clean | **broken** (98.4 % recoverable, size not a multiple of 188) | clean |
| valid KLV packets | **811** | 1185 | - | **1** in 25 MB, UL key truncated by 5 bytes |
| frames | 4860 | 7109 | - | 2866 |

It was chosen for being the shorter of the two clean recordings. Its 68
decode errors are not scattered damage: cutting the first 10 seconds off and
decoding the remainder yields **zero** errors. They are the usual artefact of
a clip cut mid-GOP, where the opening frames reference a PPS that was never
transmitted. In a looping replay that shows up for a fraction of a second
once per loop, and the KLV stream is unaffected either way.

20220930 is the fallback if a longer or completely error-free run is ever
wanted. All 811 KLV packets in the chosen clip pass their checksum and carry
21 recognised ST 0601 tags at ~5 Hz (one per six frames). Play it back with:

```
python3 testing/replay.py --file "../DECOSIS VIDEA/VIDEA_TS/20221005_071612Z_XXXX_0000045 - clip 2026-06-02 07-40-13.694.ts" --dst 239.1.1.1 --port 5000 --iface 127.0.0.1 --rate 6M --loop
```

It is deliberately **not** copied into this repository: it does not belong in
git, and it lives next to the other recordings where it was captured.

`sample.ts` stays as the synthetic counterpart, and the two are
complementary rather than redundant - between them they cover both KLV
carriages and both timing cases:

| | `sample.ts` | the real recording |
|---|---|---|
| KLV carriage | `0x15`, metadata in PES | `0x06`, private PES + `KLVA` |
| KLV PES carries PTS | yes | **no** - timing is derived |
| KLV cadence | every frame | every sixth frame |

A change to the demux or pairing code wants running against both.

**Note on the bundled `sample.ts`:** inspecting it shows every other video
access unit contains only an SEI NAL (no slice data) - two PES packets per
real frame - which does not match a transmitter that packs SEI and slice
data into one PES per frame. It still works for end-to-end testing here
(the converter counts only access units with real slice data, specifically
so this kind of split doesn't desync the PTS pairing - see "Snapshot
cadence"), but don't rely on it as a byte-for-byte reference for what a
live transmitter actually emits; recapture it against one if that matters.

## Networking requirements for a downstream consumer

`ground-pair` runs with `network_mode: host`, so it has no network
namespace of its own and cannot be addressed by Docker service name. It
sends directly to whatever address is configured. Since v3 the snapshot
channel is an outbound HTTP POST to `SNAPSHOT_API_URL`, so the consumer is
an ordinary web endpoint and no longer has to share a network with this
container - a routable URL is enough, which was the point of making it
configurable. The `RETRANSMIT` channel is still raw UDP: a downstream
container on the same machine needs to either publish its listening port
to the host (`ports: - "PORT:PORT/udp"`) or also run with
`network_mode: host`.

`GROUND_SIM_URL` is an inbound dependency rather than an output: the
simulator runs with `network_mode: host` too, so on a single machine
`http://127.0.0.1:8002` reaches it.
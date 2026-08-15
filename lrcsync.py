#!/usr/bin/python3
"""lrcsync - a terminal tap-to-sync editor for building LRC files.

Plays an audio file while you stamp timestamps onto lines of plain lyrics,
then writes a standard .lrc. Uses only the Python standard library plus
ffplay/ffprobe, which are already installed.

    ./lrcsync.py song.mp3 lyrics.txt
    ./lrcsync.py song.mp3 lyrics.txt -o out.lrc --offset -0.25

Keys:
    space        play / pause
    left/right   seek back / forward 2s (shift: 5s)
    up/down      move the cursor between lines
    enter        stamp the cursor line at the current position, advance
    backspace    clear the stamp on the cursor line
    w            write the .lrc
    q            quit (prompts if unsaved)
"""

import argparse
import curses
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SEEK_SMALL = 2.0
SEEK_LARGE = 5.0
TICK_MS = 50

AUDIO_EXTS = (".m4a", ".mp3", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma")
LRCLIB = "https://lrclib.net"
UA = "lrcsync/0.1 (personal use)"


def die(msg):
    print(f"lrcsync: {msg}", file=sys.stderr)
    sys.exit(1)


def fmt_ts(seconds):
    """Format seconds as an LRC timestamp: [mm:ss.xx]."""
    if seconds is None:
        return "  --:--.--"
    # Quantise to centiseconds *before* splitting, or 119.999 renders as
    # the invalid "01:60.00" instead of "02:00.00".
    cs = round(max(0.0, seconds) * 100)
    m, rem = divmod(cs, 6000)
    s, cs = divmod(rem, 100)
    return f"{int(m):02d}:{int(s):02d}.{int(cs):02d}"


def probe_duration(path):
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        return float(out.stdout.strip())
    except (ValueError, subprocess.SubprocessError):
        return None


def elide_middle(s, width):
    """Shorten to width by cutting the middle, keeping both ends readable."""
    if width <= 0 or len(s) <= width:
        return s
    if width <= 3:
        return "..."[:width]
    keep = width - 3
    head = (keep + 1) // 2          # favour the tail: the filename matters most
    tail = keep - head
    return s[:head] + "..." + (s[len(s) - tail:] if tail else "")


def display_path(path):
    """Relative to the working directory when that is shorter."""
    try:
        rel = os.path.relpath(str(path))
    except ValueError:
        return str(path)
    return rel if len(rel) <= len(str(path)) else str(path)


def probe_tags(path):
    """Read title/artist/album tags from the audio file."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe or not path:
        return {}
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format_tags=title,artist,album,album_artist",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        tags = json.loads(out.stdout or "{}").get("format", {}).get("tags", {})
    except (ValueError, subprocess.SubprocessError):
        return {}
    low = {k.lower(): v for k, v in tags.items()}
    return {
        "ti": low.get("title", ""),
        "ar": low.get("artist") or low.get("album_artist", ""),
        "al": low.get("album", ""),
    }


def resolve_inputs(audio_arg, lyrics_arg):
    """Given any one of the same-named files, find the rest of the set.

    "05 My Why.m4a", "05 My Why.lrc" and "05 My Why.txt" are one track, so
    naming any of them is enough to locate the audio and the lyrics source.
    """
    candidates = [p for p in (audio_arg, lyrics_arg) if p]
    if not candidates:
        return None, None
    anchor = Path(candidates[0])
    if not anchor.exists():
        die(f"no such file: {anchor}")

    base = anchor
    if anchor.suffix.lower() == ".yaml" and anchor.name.endswith(".lyricsfile.yaml"):
        base = anchor.parent / anchor.name[: -len(".lyricsfile.yaml")]
    else:
        base = anchor.with_suffix("")

    audio = audio_arg if audio_arg and Path(audio_arg).suffix.lower() in AUDIO_EXTS else None
    if not audio:
        for ext in AUDIO_EXTS:
            p = Path(str(base) + ext)
            if p.exists():
                audio = str(p)
                break

    lyrics = lyrics_arg if lyrics_arg and Path(lyrics_arg).suffix.lower() != "" \
        and Path(lyrics_arg).suffix.lower() not in AUDIO_EXTS else None
    if not lyrics:
        # An existing .lrc is preferred: it carries prior timings to edit.
        for cand in (str(base) + ".lrc", str(base) + ".txt"):
            if Path(cand).exists():
                lyrics = cand
                break
    return audio, lyrics


# ---------------------------------------------------------------------------
# LRCLIB publishing
# ---------------------------------------------------------------------------

def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def lrclib_search(artist, title, album="", instance=LRCLIB):
    """Search LRCLIB, falling back to a looser full-text query."""
    import urllib.parse as up
    tries = []
    if artist and title:
        p = {"artist_name": artist, "track_name": title}
        if album:
            tries.append(dict(p, album_name=album))
        tries.append(p)
    if title:
        tries.append({"q": f"{artist} {title}".strip()})
    # Merge every query form rather than returning the first non-empty set:
    # a synced upload may be filed under a different artist spelling
    # ("West, Matthew") that only the full-text query surfaces.
    merged, seen = [], set()
    for params in tries:
        try:
            res = _get(f"{instance}/api/search?" + up.urlencode(params))
        except Exception:
            continue
        for r in res or []:
            if r.get("id") not in seen:
                seen.add(r.get("id"))
                merged.append(r)
    return merged


def rank_results(results, duration=None):
    """Best first: synced before plain, duration match, then most complete."""
    def key(r):
        synced = bool(r.get("syncedLyrics"))
        close = bool(duration and abs((r.get("duration") or 0) - duration) <= 3)
        body = r.get("syncedLyrics") or r.get("plainLyrics") or ""
        return (not synced, not close, -len(body))
    usable = [r for r in results if r.get("syncedLyrics") or r.get("plainLyrics")]
    return sorted(usable, key=key)


def _picker(scr, results, duration):
    """Curses list of candidate lyrics. Metadata only - no lyric text."""
    curses.curs_set(0)
    scr.keypad(True)
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
    except curses.error:
        pass
    sel = 0
    while True:
        scr.erase()
        h, w = scr.getmaxyx()
        scr.addnstr(0, 0, " Choose lyrics from LRCLIB", w - 1, curses.A_BOLD)
        scr.addnstr(1, 0, f" {len(results)} candidates - file is "
                          f"{fmt_ts(duration)} long", w - 1, curses.A_DIM)
        top = 3
        view = max(1, h - 5)
        start = max(0, min(sel - view // 2, len(results) - view))
        for row, i in enumerate(range(start, min(len(results), start + view))):
            r = results[i]
            kind = "SYNCED" if r.get("syncedLyrics") else "plain "
            body = r.get("syncedLyrics") or r.get("plainLyrics") or ""
            n = len([x for x in body.splitlines() if x.strip()])
            d = r.get("duration") or 0
            flag = "=" if duration and abs(d - duration) <= 3 else " "
            line = (f"{'>' if i == sel else ' '} {kind}  {n:>3} lines  "
                    f"{int(d)//60}:{int(d)%60:02d}{flag} "
                    f"{(r.get('artistName') or '')[:20]:<20} "
                    f"{(r.get('albumName') or '')[:26]}")
            if i == sel:
                attr = curses.color_pair(1) | curses.A_BOLD
            elif r.get("syncedLyrics"):
                attr = curses.color_pair(2)
            else:
                attr = curses.A_DIM
            scr.addnstr(top + row, 0, line.ljust(w - 1)[: w - 1], w - 1, attr)
        scr.addnstr(h - 1, 0,
                    " up/down choose - enter select - q cancel"
                    "   ('=' duration matches file)", w - 1)
        scr.refresh()
        ch = scr.getch()
        if ch in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            sel = min(len(results) - 1, sel + 1)
        elif ch in (curses.KEY_ENTER, 10, 13):
            return results[sel]
        elif ch in (ord("q"), 27):
            return None


def choose_result(results, duration=None, auto=False):
    ordered = rank_results(results, duration)
    if not ordered:
        return None
    if auto or len(ordered) == 1:
        return ordered[0]
    return curses.wrapper(lambda scr: _picker(scr, ordered, duration))


def pick_result(results, duration=None):
    """Prefer synced lyrics, and a release whose duration matches the file."""
    def close(r):
        return duration and abs((r.get("duration") or 0) - duration) <= 3

    synced = [r for r in results if r.get("syncedLyrics")]
    plain = [r for r in results if r.get("plainLyrics") and not r.get("syncedLyrics")]
    for pool, kind in ((synced, "synced"), (plain, "plain")):
        if not pool:
            continue
        matched = [r for r in pool if close(r)] or pool
        best = max(matched, key=lambda r: len(r.get("syncedLyrics") or
                                              r.get("plainLyrics") or ""))
        return best, kind
    return None, None


def fetch_lyrics(base, tags, duration, instance=LRCLIB, auto=False):
    """Download a lyrics source from LRCLIB for a track with no local file.

    Synced lyrics win: they land as a .lrc that opens straight into the editor
    for correction. Plain lyrics land as a .txt that still needs tapping.
    """
    if not tags.get("ti"):
        return None, None, "audio file has no title tag to search with", {}
    results = lrclib_search(tags.get("ar", ""), tags["ti"], tags.get("al", ""),
                            instance)
    if not results:
        return None, None, "no LRCLIB match", {}
    best = choose_result(results, duration, auto=auto)
    if not best:
        return None, None, "cancelled at the picker", {}
    kind = "synced" if best.get("syncedLyrics") else "plain"
    if kind == "synced":
        path = str(base) + ".lrc"
        head = "".join(
            f"[{k}:{best.get(v) or ''}]\n"
            for k, v in (("ar", "artistName"), ("ti", "trackName"),
                         ("al", "albumName")))
        Path(path).write_text(head + best["syncedLyrics"].rstrip() + "\n",
                              encoding="utf-8")
    else:
        path = str(base) + ".txt"
        Path(path).write_text(best["plainLyrics"].rstrip() + "\n",
                              encoding="utf-8")
    n = len([x for x in (best.get("syncedLyrics") or
                         best.get("plainLyrics")).splitlines() if x.strip()])
    # Carry the chosen record's own naming forward. Publishing under these
    # values attaches to that record; publishing under a file tag that differs
    # (an "[Disc 3]" suffix, say) silently creates a separate entry instead.
    chosen = {"ar": best.get("artistName") or "",
              "ti": best.get("trackName") or "",
              "al": best.get("albumName") or ""}
    return (path, kind,
            f"{kind} lyrics from LRCLIB (id {best['id']}, {n} lines)", chosen)


def solve_challenge(prefix, target_hex):
    """Find a nonce whose SHA256(prefix+nonce) is bytewise <= target."""
    target = bytes.fromhex(target_hex)
    nonce = 0
    while True:
        if hashlib.sha256(f"{prefix}{nonce}".encode()).digest() <= target:
            return str(nonce)
        nonce += 1


def _post(url, data=None, headers=None, timeout=20):
    body = json.dumps(data).encode() if data is not None else b""
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", UA)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)


def lrclib_publish(meta, synced, plain, lyricsfile, duration,
                   progress=None, instance=LRCLIB):
    """Publish to LRCLIB. Returns (ok, message)."""
    say = progress or (lambda s: None)
    try:
        say("requesting challenge...")
        _, ch = _post(f"{instance}/api/request-challenge")
        say("solving proof of work (this can take a moment)...")
        nonce = solve_challenge(ch["prefix"], ch["target"])
        token = f"{ch['prefix']}:{nonce}"

        payload = {
            "trackName": meta.get("ti", ""),
            "artistName": meta.get("ar", ""),
            "albumName": meta.get("al", ""),
            "duration": round(float(duration or 0)),
        }
        if plain:
            payload["plainLyrics"] = plain
        if synced:
            payload["syncedLyrics"] = synced
        if lyricsfile:
            payload["lyricsfile"] = lyricsfile

        say("publishing...")
        status, _ = _post(f"{instance}/api/publish", payload,
                          {"X-Publish-Token": token})
        if status == 201:
            return True, "published to LRCLIB"
        return False, f"unexpected status {status}"
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return False, f"{body.get('name', e.code)}: {body.get('message', '')}"[:90]
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:90]


def synced_body(lines, offset=0.0):
    """Timestamped lines only, as LRCLIB expects for syncedLyrics."""
    out = []
    for l in lines:
        if not l["blank"] and l["ts"] is not None:
            out.append(f"[{fmt_ts(l['ts'] + offset)}] {l['text'].strip()}")
    return "\n".join(out)


class Player:
    """Wraps ffplay. Position is tracked on a monotonic clock because ffplay
    cannot report playhead position; seeking relaunches the process."""

    def __init__(self, path, duration=None):
        self.path = str(path)
        self.duration = duration
        self.ffplay = shutil.which("ffplay")
        self.proc = None
        self._base = 0.0        # position at which the current process started
        self._t0 = None         # monotonic clock at process start
        self.playing = False

    def position(self):
        if self._t0 is None or not self.playing:
            return self._base
        pos = self._base + (time.monotonic() - self._t0)
        if self.duration is not None:
            pos = min(pos, self.duration)
        return pos

    def _spawn(self, pos):
        self._kill()
        self._base = max(0.0, pos)
        cmd = [self.ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet",
               "-ss", f"{self._base:.3f}", self.path]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        self._t0 = time.monotonic()
        self.playing = True

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            try:
                # Resume first: a SIGSTOPped process will not act on SIGTERM.
                os.kill(self.proc.pid, signal.SIGCONT)
                os.kill(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        self.proc = None
        self._t0 = None

    def toggle(self):
        if self.playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if self.playing:
            return
        pos = self._base
        if self.proc and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.SIGCONT)
                self._t0 = time.monotonic()
                self.playing = True
                return
            except ProcessLookupError:
                pass
        self._spawn(pos)

    def pause(self):
        if not self.playing:
            return
        self._base = self.position()
        self.playing = False
        self._t0 = None
        if self.proc and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.SIGSTOP)
            except ProcessLookupError:
                self.proc = None

    def seek(self, delta):
        target = self.position() + delta
        if self.duration is not None:
            target = min(target, max(0.0, self.duration - 0.1))
        target = max(0.0, target)
        was_playing = self.playing
        self._spawn(target)
        if not was_playing:
            self.pause()

    def poll_ended(self):
        """True once playback has run past the end of the file."""
        if self.playing and self.proc and self.proc.poll() is not None:
            self._base = self.position()
            self.playing = False
            self._t0 = None
            return True
        return False

    def close(self):
        self._kill()


def load_lines(path):
    """Read plain lyrics. Blank lines become unstampable separators."""
    raw = Path(path).read_text(encoding="utf-8").splitlines()
    while raw and not raw[0].strip():
        raw.pop(0)
    while raw and not raw[-1].strip():
        raw.pop()
    return [{"text": ln.rstrip(), "ts": None, "blank": not ln.strip()} for ln in raw]


def load_existing_stamps(lines, lrc_path):
    """Re-apply timestamps from an existing .lrc so work can be resumed."""
    if not Path(lrc_path).exists():
        return 0
    pat = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$")
    entries = []
    for ln in Path(lrc_path).read_text(encoding="utf-8").splitlines():
        m = pat.match(ln.strip())
        if m:
            mins, secs, text = m.groups()
            entries.append((int(mins) * 60 + float(secs), text.strip()))

    # Match positionally, not by text: a chorus repeats the same words at
    # different times, and keying on text collapses them onto one timestamp.
    idxs = [i for i, l in enumerate(lines) if not l["blank"]]
    n = 0
    j = 0
    for ts, text in entries:
        k = j
        while k < len(idxs) and lines[idxs[k]]["text"].strip() != text:
            k += 1
        if k < len(idxs):
            lines[idxs[k]]["ts"] = ts
            n += 1
            j = k + 1
    return n


def write_lrc(path, lines, meta, offset=0.0):
    out = []
    for key in ("ar", "ti", "al", "length"):
        if meta.get(key):
            out.append(f"[{key}:{meta[key]}]")
    for line in lines:
        if line["blank"] or line["ts"] is None:
            continue
        out.append(f"[{fmt_ts(line['ts'] + offset)}] {line['text'].strip()}")
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return sum(1 for l in lines if l["ts"] is not None)


def _yaml_sq(s):
    """Single-quoted YAML scalar; only ' needs escaping, by doubling."""
    return "'" + str(s).replace("'", "''") + "'"


def write_lyricsfile(path, lines, meta, offset=0.0, plain=None):
    """Write a Lyricsfile 1.0 draft (.lyricsfile.yaml).

    end_ms is deliberately omitted: the spec says a reader should not save an
    invented end time without user input, and tap-sync only captures starts.
    """
    out = ["version: '1.0'", "", "metadata:"]
    out.append(f"  title: {_yaml_sq(meta.get('ti') or '')}")
    out.append(f"  artist: {_yaml_sq(meta.get('ar') or '')}")
    if meta.get("al"):
        out.append(f"  album: {_yaml_sq(meta['al'])}")
    if meta.get("duration_ms"):
        out.append(f"  duration_ms: {int(meta['duration_ms'])}")

    stamped = [l for l in lines if not l["blank"] and l["ts"] is not None]
    stamped.sort(key=lambda l: l["ts"])
    if stamped:
        out.append("")
        out.append("lines:")
        for line in stamped:
            ms = max(0, int(round((line["ts"] + offset) * 1000)))
            out.append(f"  - text: {_yaml_sq(line['text'].strip())}")
            out.append(f"    start_ms: {ms}")

    if plain:
        out.append("")
        out.append("plain: |")
        for ln in plain.rstrip("\n").splitlines():
            out.append(f"  {ln}".rstrip())

    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
    return len(stamped)


def lyricsfile_path_for(lrc_path):
    p = Path(lrc_path)
    return str(p.with_suffix("")) + ".lyricsfile.yaml"


def parse_lrc(lrc_path):
    """Read an .lrc directly into (lines, meta). Used by --from-lrc, where the
    .lrc is the sole source of both text and timings, so no text matching is
    needed and repeated lines cannot be confused."""
    ts_pat = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$")
    meta_pat = re.compile(r"^\[(ar|ti|al|length):(.*)\]$")
    lines, meta, dropped = [], {}, 0
    for raw in Path(lrc_path).read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        m = meta_pat.match(raw)
        if m:
            meta[m.group(1)] = m.group(2).strip()
            continue
        m = ts_pat.match(raw)
        if not m:
            continue
        text = m.group(3).strip()
        if not text:
            dropped += 1          # bare timing marker for an instrumental gap
            continue
        lines.append({"text": text, "blank": False,
                      "ts": int(m.group(1)) * 60 + float(m.group(2))})
    return lines, meta, dropped


def save_all(lines, out_path, meta, offset, formats, plain=None):
    """Write every requested format. Returns [(count, path), ...]."""
    written = []
    if "lrc" in formats:
        n = write_lrc(out_path, lines, meta, offset)
        written.append((n, out_path))
    if "lyricsfile" in formats:
        p = lyricsfile_path_for(out_path)
        n = write_lyricsfile(p, lines, meta, offset, plain)
        written.append((n, p))
    return written


class App:
    def __init__(self, stdscr, player, lines, out_path, meta, offset,
                 formats=("lrc",), plain=None):
        self.scr = stdscr
        self.player = player
        self.lines = lines
        self.out_path = out_path
        self.meta = meta
        self.offset = offset
        self.formats = formats
        self.plain = plain
        self.cursor = next((i for i, l in enumerate(lines) if not l["blank"]), 0)
        self.dirty = False
        self.status = ("space play - enter stamp - w write - m metadata"
                       " - u upload - q quit")
        self.pending = None          # None | "quit" | "upload"
        self.upload_thread = None
        self.status_path = None      # elided to fit at draw time, never wrapped

    # -- helpers -----------------------------------------------------------
    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        # A plain assignment is a new message, so any previous path is stale.
        self._status = value
        self.status_path = None

    def set_status(self, text, path=None):
        """Status text, optionally naming a file. The path is quoted, and
        elided in the middle only when the terminal is too narrow for it."""
        self.status = text
        self.status_path = display_path(path) if path else None

    def rendered_status(self, avail):
        if not self.status_path:
            return self.status[:avail]
        full = f"{self.status} '{self.status_path}'"
        if len(full) <= avail:
            return full
        budget = avail - len(self.status) - 3      # space plus both quotes
        return f"{self.status} '{elide_middle(self.status_path, budget)}'"

    def stampable(self):
        return [i for i, l in enumerate(self.lines) if not l["blank"]]

    def move_cursor(self, step):
        idxs = self.stampable()
        if not idxs:
            return
        if self.cursor in idxs:
            pos = idxs.index(self.cursor)
            pos = max(0, min(len(idxs) - 1, pos + step))
        else:
            pos = 0
        self.cursor = idxs[pos]

    def stamp(self):
        line = self.lines[self.cursor]
        if line["blank"]:
            self.move_cursor(1)
            return
        line["ts"] = self.player.position()
        self.dirty = True
        self.status = f"stamped line {self.cursor + 1} at {fmt_ts(line['ts'])}"
        idxs = self.stampable()
        if self.cursor == idxs[-1]:
            self.status += "  (last line - press w to write)"
        else:
            self.move_cursor(1)

    def unstamp(self):
        line = self.lines[self.cursor]
        if line["ts"] is not None:
            line["ts"] = None
            self.dirty = True
            self.status = f"cleared line {self.cursor + 1}"

    def _prompt(self, y, x, initial, width):
        """Inline single-line editor. Returns the new string, or None on Esc."""
        buf = list(initial)
        curses.curs_set(1)
        try:
            while True:
                shown = "".join(buf)[-width:] if width > 0 else ""
                self.scr.addnstr(y, x, shown.ljust(width)[:width], width,
                                 curses.A_UNDERLINE)
                self.scr.move(y, x + min(len(shown), max(0, width - 1)))
                self.scr.refresh()
                ch = self.scr.getch()
                if ch in (curses.KEY_ENTER, 10, 13):
                    return "".join(buf).strip()
                if ch == 27:                      # Esc discards the edit
                    return None
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    if buf:
                        buf.pop()
                elif 32 <= ch < 127:
                    buf.append(chr(ch))
        finally:
            curses.curs_set(0)

    def edit_metadata(self):
        """Edit the title/artist/album an upload will be filed under."""
        fields = [("ti", "Title"), ("ar", "Artist"), ("al", "Album")]
        sel = 0
        before = {k: self.meta.get(k, "") for k, _ in fields}
        self.scr.timeout(-1)                      # block: this is a text form
        try:
            while True:
                self.scr.erase()
                h, w = self.scr.getmaxyx()
                self.scr.addnstr(0, 0, " Edit metadata", w - 1, curses.A_BOLD)
                self.scr.addnstr(1, 0, " these values are what an upload files "
                                       "the track under", w - 1, curses.A_DIM)
                for i, (key, label) in enumerate(fields):
                    y = 3 + i * 2
                    mark = ">" if i == sel else " "
                    self.scr.addnstr(y, 0, f" {mark} {label:<7}", w - 1,
                                     curses.color_pair(3) | curses.A_BOLD
                                     if i == sel else curses.A_NORMAL)
                    val = self.meta.get(key, "") or "(empty)"
                    self.scr.addnstr(y, 12, val[: max(0, w - 13)], max(0, w - 13),
                                     curses.A_NORMAL if self.meta.get(key)
                                     else curses.A_DIM)
                self.scr.addnstr(h - 1, 0,
                                 " up/down field - enter edit - esc cancel edit"
                                 " - q done", w - 1)
                self.scr.refresh()
                ch = self.scr.getch()
                if ch == curses.KEY_UP:
                    sel = max(0, sel - 1)
                elif ch == curses.KEY_DOWN:
                    sel = min(len(fields) - 1, sel + 1)
                elif ch in (curses.KEY_ENTER, 10, 13):
                    key, label = fields[sel]
                    y = 3 + sel * 2
                    new = self._prompt(y, 12, self.meta.get(key, ""),
                                       max(4, w - 13))
                    if new is not None:
                        self.meta[key] = new
                elif ch in (ord("q"), 27):
                    break
        finally:
            self.scr.timeout(TICK_MS)
        changed = [l for k, l in fields if before[k] != self.meta.get(k, "")]
        if changed:
            self.dirty = True
            self.status = f"metadata updated ({', '.join(changed)}) - w to write"
        else:
            self.status = "metadata unchanged"

    def upload(self):
        """Validate, then ask for confirmation. Publishing is public and
        cannot be undone, so it never fires on a single keypress."""
        if self.upload_thread and self.upload_thread.is_alive():
            self.status = "upload already in progress"
            return
        missing = [l for l in self.lines if not l["blank"] and l["ts"] is None]
        if missing:
            self.status = f"{len(missing)} lines still unstamped - not uploading"
            return
        if not self.meta.get("ti") or not self.meta.get("ar"):
            self.status = "need a title and artist to upload (--title/--artist)"
            return
        self.pending = "upload"
        self.status = f"publish '{self.meta['ti']}' to LRCLIB? y to confirm, n to cancel"

    def do_upload(self):
        self.save()                      # publish exactly what is on disk
        synced = synced_body(self.lines, self.offset)
        plain = self.plain or "\n".join(
            l["text"] for l in self.lines if not l["blank"])
        yml = Path(lyricsfile_path_for(self.out_path))
        lyricsfile = yml.read_text(encoding="utf-8") if yml.exists() else None
        dur = self.player.duration
        meta = dict(self.meta)

        def work():
            ok, msg = lrclib_publish(
                meta, synced, plain, lyricsfile, dur,
                progress=lambda s: setattr(self, "status", s))
            self.status = ("uploaded - " if ok else "upload failed - ") + msg

        self.upload_thread = threading.Thread(target=work, daemon=True)
        self.upload_thread.start()

    def save(self):
        written = save_all(self.lines, self.out_path, self.meta, self.offset,
                           self.formats, self.plain)
        self.dirty = False
        off = f" (offset {self.offset:+.2f}s)" if self.offset else ""
        n = written[0][0] if written else 0
        extra = f" (+{len(written) - 1} more)" if len(written) > 1 else ""
        self.set_status(f"wrote {n} lines to{extra}{off}",
                        written[0][1] if written else None)

    # -- drawing -----------------------------------------------------------
    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 8 or w < 40:
            scr.addnstr(0, 0, "terminal too small", w - 1)
            scr.refresh()
            return

        pos = self.player.position()
        dur = self.player.duration
        state = "PLAYING" if self.player.playing else "PAUSED "

        # Header: the big live timestamp is what you stamp at.
        # Header names the metadata an upload would carry, so a wrong album
        # or a missing artist is visible before pressing u.
        title = self.meta.get("ti") or Path(self.player.path).name
        head = f" {title}"
        scr.addnstr(0, 0, head[: w - 1], w - 1, curses.A_BOLD)
        extras = "  -  ".join(x for x in (self.meta.get("ar"),
                                          self.meta.get("al")) if x)
        if not self.meta.get("ar"):
            extras = "(no artist - cannot upload)" + ("  " + extras if extras else "")
        room = w - len(head) - 2
        if extras and room > 6:
            scr.addnstr(0, len(head) + 1, elide_middle(extras, room), room,
                        curses.A_DIM)
        head = f" MARK AT  {fmt_ts(pos)}"
        scr.addnstr(1, 0, head, w - 1, curses.color_pair(1) | curses.A_BOLD)
        tail = f"{state}  of {fmt_ts(dur)}" if dur else state
        if w > len(head) + len(tail) + 2:
            attr = curses.color_pair(2) if self.player.playing else curses.A_DIM
            scr.addnstr(1, w - len(tail) - 1, tail, len(tail), attr)

        # Progress bar.
        if dur:
            barw = max(10, w - 2)
            filled = int(barw * min(1.0, pos / dur)) if dur else 0
            scr.addnstr(2, 1, "-" * filled + "." * (barw - filled), barw,
                        curses.A_DIM)

        top, bottom = 4, h - 3
        view = bottom - top
        done = sum(1 for l in self.lines if l["ts"] is not None)
        total = len(self.stampable())

        # Keep the cursor centred in the visible window.
        start = max(0, min(self.cursor - view // 2, len(self.lines) - view))
        for row, i in enumerate(range(start, min(len(self.lines), start + view))):
            line = self.lines[i]
            y = top + row
            sel = i == self.cursor
            if line["blank"]:
                scr.addnstr(y, 0, "   " + "-" * 8, w - 1, curses.A_DIM)
                continue
            ts = f"[{fmt_ts(line['ts'])}]" if line["ts"] is not None else "[  --:--.--]"
            marker = ">" if sel else " "
            text = line["text"].strip()
            body = f"{marker} {ts} {text}"
            if sel:
                attr = curses.color_pair(3) | curses.A_BOLD
            elif line["ts"] is not None:
                attr = curses.color_pair(2)
            else:
                attr = curses.A_DIM
            scr.addnstr(y, 0, body.ljust(w - 1)[: w - 1], w - 1, attr)

        # Footer.
        prog = f" {done}/{total} stamped"
        if self.dirty:
            prog += "  *unsaved*"
        scr.addnstr(h - 2, 0, prog, w - 1, curses.A_BOLD)
        scr.addnstr(h - 1, 0, " " + self.rendered_status(w - 2), w - 1,
                    curses.color_pair(4) if self.pending else curses.A_NORMAL)
        scr.refresh()

    # -- main loop ---------------------------------------------------------
    def run(self):
        curses.curs_set(0)
        # timeout() rather than nodelay(): a non-blocking read hands back a
        # bare ESC before the rest of "ESC [ D" arrives, so arrow keys never
        # assemble into KEY_LEFT/KEY_RIGHT.
        self.scr.timeout(TICK_MS)
        self.scr.keypad(True)
        while True:
            self.player.poll_ended()
            self.draw()
            try:
                ch = self.scr.getch()
            except KeyboardInterrupt:
                break
            if ch == -1:
                continue

            if self.pending and ch not in (ord("y"), ord("n")):
                self.pending = None

            if ch == ord("y") and self.pending:
                if self.pending == "quit":
                    break
                self.pending = None
                self.do_upload()
                continue
            elif ch == ord("n") and self.pending:
                self.pending = None
                self.status = "cancelled"
                continue

            if ch in (ord(" "),):
                self.player.toggle()
            elif ch in (curses.KEY_ENTER, 10, 13):
                self.stamp()
            elif ch == curses.KEY_LEFT:
                self.player.seek(-SEEK_SMALL)
            elif ch == curses.KEY_RIGHT:
                self.player.seek(SEEK_SMALL)
            elif ch == curses.KEY_SLEFT:
                self.player.seek(-SEEK_LARGE)
            elif ch == curses.KEY_SRIGHT:
                self.player.seek(SEEK_LARGE)
            elif ch == curses.KEY_UP:
                self.move_cursor(-1)
            elif ch == curses.KEY_DOWN:
                self.move_cursor(1)
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.unstamp()
            elif ch == ord("w"):
                self.save()
            elif ch == ord("u"):
                self.upload()
            elif ch == ord("m"):
                self.edit_metadata()
            elif ch == ord("q"):
                if self.upload_thread and self.upload_thread.is_alive():
                    self.status = "upload in progress - wait before quitting"
                elif self.dirty:
                    self.pending = "quit"
                    self.status = "unsaved changes - press y to quit, n to stay"
                else:
                    break
            elif ch == curses.KEY_RESIZE:
                continue


def convert_from_lrc(args):
    """Emit a .lyricsfile.yaml straight from an existing .lrc."""
    src = args.from_lrc
    if not Path(src).exists():
        die(f"no such file: {src}")
    lines, lrc_meta, dropped = parse_lrc(src)
    if not lines:
        die(f"no timestamped lines found in {src}")

    meta = {
        "ti": args.title or lrc_meta.get("ti", ""),
        "ar": args.artist or lrc_meta.get("ar", ""),
        "al": args.album or lrc_meta.get("al", ""),
    }
    if not meta["ti"] or not meta["ar"]:
        die("lyricsfile requires title and artist (pass --title/--artist)")

    if args.audio:
        duration = probe_duration(args.audio)
        if duration:
            meta["duration_ms"] = int(round(duration * 1000))

    out = args.out or lyricsfile_path_for(src)
    if not out.endswith(".lyricsfile.yaml"):
        out = lyricsfile_path_for(out)
    plain = "\n".join(l["text"] for l in lines)
    n = write_lyricsfile(out, lines, meta, args.offset, plain)
    note = f"  ({dropped} bare timing marker dropped)" if dropped else ""
    print(f"wrote {n} lines: {out}{note}")


def main():
    ap = argparse.ArgumentParser(description="Tap-to-sync LRC editor.")
    ap.add_argument("audio", nargs="?", help="audio file to play")
    ap.add_argument("lyrics", nargs="?",
                    help="plain text lyrics, one line per lyric line")
    ap.add_argument("-o", "--out", help="output .lrc (default: alongside lyrics)")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="seconds added to every stamp on write, e.g. -0.25")
    ap.add_argument("--artist", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--album", default="")
    ap.add_argument("--format", default="both",
                    choices=["lrc", "lyricsfile", "both"],
                    help="output format(s) to write (default: both)")
    ap.add_argument("--convert", action="store_true",
                    help="skip the editor: re-emit stamps from an existing "
                         ".lrc in the requested format(s)")
    ap.add_argument("--auto", action="store_true",
                    help="skip the picker: take the best LRCLIB match")
    ap.add_argument("--no-fetch", action="store_true",
                    help="never query LRCLIB for a missing lyrics source")
    ap.add_argument("--from-lrc", metavar="FILE",
                    help="convert an existing .lrc directly (text and timings "
                         "both come from it; no lyrics .txt needed)")
    args = ap.parse_args()

    if args.from_lrc:
        return convert_from_lrc(args)

    formats = ("lrc", "lyricsfile") if args.format == "both" else (args.format,)
    audio, lyrics = resolve_inputs(args.audio, args.lyrics)
    if not audio and not lyrics:
        die("nothing found for that name")
    if not audio:
        die("no audio file found next to " + str(lyrics))

    fetched_meta = {}
    if not lyrics and not args.no_fetch:
        # Bare audio file: pull a lyrics source from LRCLIB using its tags.
        tags0 = probe_tags(audio)
        for k, flag in (("ar", args.artist), ("ti", args.title), ("al", args.album)):
            if flag:
                tags0[k] = flag
        base = Path(audio).with_suffix("")
        lyrics, kind, note, fetched_meta = fetch_lyrics(
            base, tags0, probe_duration(audio), auto=args.auto)
        print(f"lrclib: {note}")
        if not lyrics:
            die("no lyrics source found; supply a .txt or use --title/--artist")
    if not lyrics:
        die("no lyrics source found (.lrc or .txt beside the audio)")
    if not args.convert and not shutil.which("ffplay"):
        die("ffplay not found on PATH (brew install ffmpeg)")

    out_path = args.out or str(Path(lyrics).with_suffix(".lrc"))
    editing_lrc = Path(lyrics).suffix.lower() == ".lrc"

    lrc_meta = {}
    if editing_lrc:
        # Opening an existing .lrc: it supplies both the text and the timings.
        lines, lrc_meta, _ = parse_lrc(lyrics)
        if not lines:
            die(f"no timestamped lines found in {lyrics}")
        resumed = sum(1 for l in lines if l["ts"] is not None)
        plain = "\n".join(l["text"] for l in lines)
    else:
        lines = load_lines(lyrics)
        if not any(not l["blank"] for l in lines):
            die("no lyric lines found")
        resumed = load_existing_stamps(lines, out_path)
        plain = Path(lyrics).read_text(encoding="utf-8")

    duration = probe_duration(audio)
    tags = probe_tags(audio)
    # Precedence: explicit flags, then the LRCLIB record this came from, then
    # the .lrc's own tags, then the audio's tags. The fetched record outranks
    # the file tags so an upload attaches to that record rather than forking
    # a near-duplicate over a naming difference.
    meta = {}
    for key, flag in (("ar", args.artist), ("ti", args.title), ("al", args.album)):
        meta[key] = (flag or fetched_meta.get(key) or lrc_meta.get(key)
                     or tags.get(key, ""))
    if duration:
        meta["length"] = f"{int(duration) // 60}:{int(duration) % 60:02d}"
        meta["duration_ms"] = int(round(duration * 1000))
    args.audio, args.lyrics = audio, lyrics

    if args.convert:
        if not resumed:
            die(f"no existing stamps found in {out_path}")
        for n, p in save_all(lines, out_path, meta, args.offset, formats, plain):
            print(f"wrote {n} lines: {p}")
        return

    player = Player(args.audio, duration)
    try:
        app = curses.wrapper(lambda scr: _boot(scr, player, lines, out_path,
                                               meta, args.offset, resumed,
                                               formats, plain,
                                               lyrics if editing_lrc else out_path))
    finally:
        player.close()

    if app and app.dirty:
        print("quit with unsaved stamps (nothing written)")
    else:
        for f in formats:
            print(f"{f}: {out_path if f == 'lrc' else lyricsfile_path_for(out_path)}")


def _boot(scr, player, lines, out_path, meta, offset, resumed,
          formats=("lrc",), plain=None, resumed_from=None):
    # Arrow keys arrive as escape sequences; ncurses otherwise waits a full
    # second to disambiguate a bare ESC, which makes seeking feel dead.
    try:
        curses.set_escdelay(25)
    except AttributeError:
        pass
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    app = App(scr, player, lines, out_path, meta, offset, formats, plain)
    if resumed:
        app.set_status(f"resumed {resumed} stamps from existing",
                       resumed_from or out_path)
    app.run()
    return app


if __name__ == "__main__":
    main()

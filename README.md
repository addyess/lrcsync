# lrcsync

A terminal tap-to-sync editor for building synced lyrics files.

Play a track, tap `enter` on each line as it's sung, and get a timestamped
`.lrc` — plus a [Lyricsfile](https://github.com/tranxuanthang/lyricsfile) YAML —
written next to your audio. When a track has no local lyrics, it fetches them
from [LRCLIB](https://lrclib.net); when you're done, `u` publishes your work
back so the next person doesn't have to tap it out.

Single file, standard library only. No pip install, no packages, no account.

## Why

LRCLIB has plain lyrics for far more songs than it has synced ones. The
official client, [LRCGET](https://github.com/tranxuanthang/lrcget), has an
excellent editor for closing that gap, but it's a desktop app you have to
install. This is the same idea in a terminal, in one readable file you can
audit before running.

## Requirements

- Python 3.8+ (uses only the standard library, including `curses`)
- `ffplay` and `ffprobe` — both ship with [ffmpeg](https://ffmpeg.org)
  - macOS: `brew install ffmpeg`
  - Debian/Ubuntu: `apt install ffmpeg`

`ffplay` handles playback and seeking; `ffprobe` reads duration and tags.

## Install

```sh
curl -O https://raw.githubusercontent.com/addyess/lrcsync/main/lrcsync.py
chmod +x lrcsync.py
```

## Usage

The common case is one argument:

```sh
./lrcsync.py "05 My Why.m4a"
```

That reads the file's tags, looks for a lyrics source, and opens the editor.

Files sharing a basename are treated as one track, so naming any of them works:

```
05 My Why.m4a                 audio
05 My Why.txt                 plain lyrics, needs syncing
05 My Why.lrc                 synced output
05 My Why.lyricsfile.yaml     synced output, YAML
```

### Where lyrics come from

In order of preference:

1. **An existing `.lrc`** beside the audio — opens with its timings loaded for
   correction.
2. **A `.txt`** beside the audio — one lyric line per line, ready to tap.
3. **LRCLIB** — searched using the audio's tags. Synced results are offered
   first (they land as a `.lrc` you can edit); plain results land as a `.txt`
   that still needs tapping.

When LRCLIB returns more than one candidate, a picker shows each one's line
count, duration, artist and album so you can tell near-identical releases
apart. A `=` marks a duration matching your file.

## Keys

### Editor

| Key | Action |
| --- | --- |
| `space` | play / pause |
| `enter` | stamp the current line, advance |
| `←` / `→` | seek 2s back / forward (`shift` for 5s) |
| `↑` / `↓` | move between lines |
| `backspace` | clear the current line's stamp |
| `w` | write output files |
| `m` | edit title / artist / album |
| `u` | publish to LRCLIB |
| `q` | quit (prompts if unsaved) |

The header shows a live `MARK AT 01:23.45` — exactly what `enter` will stamp —
along with the metadata an upload would be filed under.

### Picker

`↑`/`↓` or `j`/`k` to move, `enter` to select, `q` to cancel.

### Metadata form

`↑`/`↓` between fields, `enter` to edit one, `esc` to cancel that edit,
`q` when done.

## Fixing timing drift

Timestamps come from a monotonic clock started when playback begins, so there's
a small constant startup latency. If playback consistently leads or lags:

```sh
./lrcsync.py "05 My Why.m4a" --offset -0.2
```

Press `w` again. Every stamp is shifted on write — no re-tapping.

## Publishing

`u` publishes to LRCLIB. It refuses if any line is unstamped or if title or
artist are missing, then asks for confirmation naming the track, because
publishing is public and permanent.

There's no account and no API key. LRCLIB uses a proof of work: it hands back a
`prefix` and a `target`, and the client hashes `prefix + nonce` with SHA-256,
incrementing until the result is numerically at or below the target. That takes
a few seconds of CPU, which is trivial once and expensive at spam volume. The
solve runs on a background thread so the editor stays responsive.

**Match the album to an existing record.** LRCLIB identifies a track by artist,
title, album *and* duration. If your file's album tag reads
`Greatest Hits [Disc 2]` but the existing record says `Greatest Hits`, you'll
create a second entry rather than adding synced lyrics to the one people find.
Metadata from a picked LRCLIB record is preferred over your file's tags for
exactly this reason, and `m` lets you correct it before publishing.

## Output formats

Both are written by default:

- **`.lrc`** — the classic bracket format, `[00:12.34] a line`
- **`.lyricsfile.yaml`** — the [Lyricsfile 1.0 draft](https://github.com/tranxuanthang/lyricsfile),
  a YAML format supported by LRCGET and LRCLIB

`end_ms` is deliberately omitted from the YAML. Tap-syncing captures line
*starts*, and the spec says a reader shouldn't save an invented end time
without user input. Deriving each end from the next line's start would assert
that lines run continuously, which is wrong across instrumental breaks.

Use `--format lrc` or `--format lyricsfile` for just one.

## Options

| Flag | Effect |
| --- | --- |
| `-o`, `--out` | output path (default: alongside the lyrics source) |
| `--offset` | seconds added to every stamp on write, e.g. `-0.25` |
| `--artist`, `--title`, `--album` | override metadata |
| `--format` | `lrc`, `lyricsfile`, or `both` (default) |
| `--auto` | skip the picker, take the best LRCLIB match |
| `--no-fetch` | never query LRCLIB for a missing lyrics source |
| `--convert` | skip the editor, re-emit an existing `.lrc` in other formats |
| `--from-lrc FILE` | convert a `.lrc` directly; no lyrics `.txt` needed |

Metadata precedence: explicit flags → the LRCLIB record you picked → the
`.lrc`'s own tags → the audio file's tags.

## Notes and limitations

- Seeking relaunches `ffplay` at the new position, since it can't report its
  playhead. This is fast but not sample-accurate.
- Blank lines in a `.txt` are treated as stanza separators — displayed, but not
  stampable and not written to output.
- Repeated lines (choruses) are matched positionally, not by text, so each
  occurrence keeps its own timing.
- Terminal must be at least 40x8.

## Lyrics and copyright

This tool doesn't ship lyrics. It reads local files you supply and queries
LRCLIB, whose contents are contributed by its users. Song lyrics are generally
copyrighted by their authors and publishers; syncing a copy for personal use is
a different thing from redistributing one. Publishing to LRCLIB shares your
timings — and the lyric text — publicly, so use your own judgement about what
you upload.

## Credits

Built around [LRCLIB](https://lrclib.net) and the
[Lyricsfile](https://github.com/tranxuanthang/lyricsfile) format, both by
[tranxuanthang](https://github.com/tranxuanthang), who also wrote
[LRCGET](https://github.com/tranxuanthang/lrcget). The publish protocol here
follows LRCGET's implementation.

## License

MIT

# dirsync

A zero-dependency, interactive file synchronizer for any two directories — no cloud, no daemon, no external libraries.

## Features

- **Three sync directions**: Source → Destination, Destination → Source, or bidirectional "smart" sync (newest file wins)
- **Zero dependencies** — pure Python standard library, works offline
- **Fast change detection** — size check first, MD5 hash only when needed, computed in parallel across a bounded thread pool
- **Atomic file writes** — every copy goes through a temp file + rename, so a killed process never leaves a partial file behind
- **Dry-run mode** — preview every change before anything is touched
- **Manual file picking** — choose exactly which detected changes to apply
- **Extension filtering** — sync only `.jsx`, `.css`, `.js`, etc.
- **Path-aware ignore patterns** — plain names (`node_modules`) match anywhere in the tree; patterns with a `/` (`exports/projects`) match only that exact relative path
- **Multi-pattern entry** — add several ignore patterns at once, comma-separated
- **Watch mode** — re-sync automatically on an interval
- **Full & focus backups** — snapshot the whole target tree, or only the files about to be overwritten/deleted, before every sync
- **Backup browser & restore** — list past backups and restore any of them, with a two-step typed confirmation before anything destructive happens
- **Mass-deletion guard** — refuses to wipe a destination if the source scan comes back suspiciously empty
- **PID lock file** — prevents two instances from syncing the same config at once
- **Android/Termux aware** — config, log, lock, and backups automatically move to Termux's private home when the script runs from shared storage, avoiding FUSE write issues
- **Cross-platform** — Linux, macOS, Windows, Android (Termux)
- **Sync history log** — optional append-only log of every sync pass

## Requirements

- Python 3.8+
- No third-party packages — standard library only

## Project Structure

```
dirsync/
└── dirsync.py    # single-file tool — everything lives here
```

## Installation

**Option 1 — direct download:**

```bash
curl -O https://raw.githubusercontent.com/fuegodev369/dirsync/main/dirsync.py
python3 dirsync.py
```

**Option 2 — clone the repo:**

```bash
git clone https://github.com/fuegodev369/dirsync.git
cd dirsync
python3 dirsync.py
```

No `pip install` step — the script is self-contained.

## Usage

```bash
python dirsync.py                          # interactive mode
```

| Flag | Description |
|---|---|
| `--dry-run` | Simulate the sync — no files are modified |
| `--auto` | Skip the confirmation prompt (non-interactive) |
| `--direction {src,dst,smart}` | `src`=Source→Dest (default), `dst`=Dest→Source, `smart`=bidirectional |
| `--pick` | Manually select which files to sync |
| `--ext EXT [EXT ...]` | Filter by file extension(s), e.g. `--ext jsx css js` |
| `--watch SECONDS` | Watch mode — repeat the sync every N seconds |
| `--log` | Append sync history to the log file |
| `--source PATH` | Override the source path for this run only |
| `--dest PATH` | Override the destination path for this run only |
| `--no-color` | Disable ANSI color output (useful for pipes and CI) |
| `--config` | Open the settings menu |
| `--backups` | Open the backup browser/restore manager |
| `--version` | Show the version number |
| `--help` | Show the full help text |

## Interactive flow example

```
╔═══════════════════════════════════════╗
║      dirsync  v2.1.0  by FuegoDev     ║
║    Two-directory file synchronizer    ║
╚═══════════════════════════════════════╝

── Current configuration ──
  Source          : /home/user/projects/app
  Destination     : /mnt/backup/app
  Ignored         : node_modules, .git, dist, build, __pycache__, *.log
  Delete orphans  : No
  Auto backup     : No
  Backup mode     : full

── Main menu ──
  [1] Start a sync
  [2] Settings
  [3] Backup manager
  [0] Quit

Choice: 1

── Sync direction ──
  [1] Source  ➜  Destination   (default)
  [2] Destination  ➜  Source   (reverse)
  [3] Bidirectional smart       (newest wins)

Direction (1/2/3) [1]: 1

Scanning files...
  Source      : 128 file(s)
  Destination : 124 file(s)

── 5 change(s) detected ──

  [+] 4 new file(s):
       + src/utils/format.js
         Added on : 2026-08-14  22:10:05

  [~] 1 modified file(s):
       ~ src/index.js
         Source      modified : 2026-08-15  09:02:11
         Destination modified : 2026-08-14  20:15:44

Apply these changes? (y/n): y

── Syncing files... ──
  [+] src/utils/format.js
  [~] src/index.js

── Summary ──
  ✓ 5 operation(s) completed — 2026-08-15  09:10:32
```

## Output example

With `--log`, every sync pass appends an entry like this to `.dirsync.log`:

```
[2026-08-15 09:10:32]
  source      : /home/user/projects/app
  destination : /mnt/backup/app
  direction   : src
  copied      : 5
  deleted     : 0
  success     : 5
  errors      : 0
```

## Default behavior table

| Setting | Default | Notes |
|---|---|---|
| Sync direction | `src` (Source → Destination) | Change with `--direction` |
| Delete orphans | Disabled | Enable via `--config` |
| Auto backup before sync | Disabled | Enable via `--config` |
| Backup mode | `full` | `full` copies the whole tree, `focus` only backs up at-risk files |
| Color output | Enabled | Disabled automatically on non-TTY output or with `--no-color` |
| Config/log location | Next to the script | Moves to the Termux home automatically on Android shared storage |

## Contributing

Issues and pull requests are welcome. Please keep the tool dependency-free — no third-party packages, no CDN scripts, nothing that breaks offline use.

## License

MIT © FuegoDev

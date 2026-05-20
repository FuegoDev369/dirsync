# 🔄 dirsync

> **Sync any two directories — interactive, smart, zero dependencies.**

`dirsync.py` is a single-file Python script that keeps two directories in sync. It detects changes using **MD5 content hashing** (not timestamps, which can be unreliable), supports bidirectional sync, watch mode, and file-level manual selection — all with no third-party packages required.

Works on **Linux, macOS, Windows, and Android (Termux)**.

---

## ✨ Features

- **Zero dependencies** — pure Python standard library
- **MD5-based detection** — reliable on any filesystem, including Android
- **3 sync directions** — source→dest, dest→source, or smart bidirectional
- **Watch mode** — automatically re-sync every N seconds
- **Manual pick mode** — choose exactly which files to sync
- **Extension filter** — sync only `.jsx`, `.css`, `.py`, etc.
- **Auto-backup** — copy destination before any sync
- **Orphan control** — optionally delete files missing from source
- **Sync log** — persistent history of all sync operations
- **First-run wizard** — guided setup on first launch
- **Cross-platform** — Windows, macOS, Linux, Android/Termux

---

## 📋 Requirements

- Python **3.6+**
- No third-party packages required

---

## 🚀 Installation

No installation needed. Just download and run.

**Option 1 — Download directly:**

```bash
curl -O https://raw.githubusercontent.com/fuegodev369/dirsync/main/dirsync.py
```

**Option 2 — Clone the repo:**

```bash
git clone https://github.com/fuegodev369/dirsync.git
cd dirsync
```

**Make executable (Linux / macOS / Termux):**

```bash
chmod +x dirsync.py
```

---

## 💻 Usage

### Basic interactive sync

```bash
python dirsync.py
```

On first run, a wizard will ask you for your source and destination paths. Settings are saved to `~/.dirsync_config.json`.

### Override paths on the fly

```bash
python dirsync.py --source /path/to/a --dest /path/to/b
```

### Simulate before committing

```bash
python dirsync.py --dry-run
```

### Sync automatically without prompts

```bash
python dirsync.py --auto
```

### Watch mode — auto-sync every N seconds

```bash
python dirsync.py --watch 5      # sync every 5 seconds
python dirsync.py --watch 30     # sync every 30 seconds
```

### Bidirectional sync (newest file wins)

```bash
python dirsync.py --direction smart
```

### Reverse sync (destination → source)

```bash
python dirsync.py --direction dst
```

### Pick files manually

```bash
python dirsync.py --pick
```

### Filter by file extension

```bash
python dirsync.py --ext jsx css js
python dirsync.py --ext py
```

### Save sync history to a log file

```bash
python dirsync.py --log
```

Log is saved to `~/.dirsync.log`.

### Open settings menu

```bash
python dirsync.py --config
```

---

## 📊 All options

| Option | Description |
|---|---|
| `--dry-run` | Simulate — no files modified |
| `--auto` | Skip confirmation prompt |
| `--config` | Open the settings menu |
| `--direction src` | Source → Destination (default) |
| `--direction dst` | Destination → Source |
| `--direction smart` | Bidirectional — newest file wins |
| `--pick` | Manually select which files to sync |
| `--ext jsx css` | Sync only specific file extensions |
| `--watch N` | Repeat sync every N seconds |
| `--log` | Append sync history to `~/.dirsync.log` |
| `--source /path` | Override source path for this run |
| `--dest /path` | Override destination path for this run |
| `--version` | Show version number |
| `--help` | Show help message |

---

## 🎬 Interactive flow example

```
╔══════════════════════════════════════════════════════════╗
║           dirsync  v1.0.0  by FuegoDev                  ║
║       Two-directory file synchronizer                   ║
╚══════════════════════════════════════════════════════════╝

── Current configuration ──
  Source          : /storage/shared/MyProject
  Destination     : ~/MyProject
  Ignored         : node_modules, .git, dist, build ...
  Delete orphans  : No
  Auto backup     : No

── Sync direction ──
  [1] Source  ➜  Destination   (default)
  [2] Destination  ➜  Source   (reverse)
  [3] Bidirectional smart       (newest wins)

Direction (1/2/3) [1]: 1

Scanning files...
  Source      : 24 file(s)
  Destination : 22 file(s)

  Mode: Source  ➜  Destination

── 3 change(s) detected ──

  [+] 1 new file(s):
       + components/Button.jsx
         Added on : 2024-11-12  14:30:01

  [~] 2 modified file(s):
       ~ App.jsx
         Source      modified : 2024-11-12  14:28:44
         Destination modified : 2024-11-11  09:12:00
       ~ index.css
         Source      modified : 2024-11-12  13:55:20
         Destination modified : 2024-11-10  18:03:11

Apply these changes? (y/n):
➜ y

── Syncing files... ──
  [+] components/Button.jsx
  [~] App.jsx
  [~] index.css

── Summary ──
  ✓ 3 operation(s) completed — 2024-11-12  14:32:05
```

---

## ⚙️ Configuration file

Settings are stored at `~/.dirsync_config.json`:

```json
{
  "source": "/path/to/source",
  "destination": "/path/to/destination",
  "ignore_patterns": [
    "node_modules", ".git", ".vite",
    "dist", "build", ".cache",
    "__pycache__", "*.log",
    "package-lock.json", ".DS_Store", "Thumbs.db"
  ],
  "delete_orphans": false,
  "backup_before_sync": false
}
```

| Key | Description |
|---|---|
| `source` | Source directory path |
| `destination` | Destination directory path |
| `ignore_patterns` | Folder/file names or patterns to always skip |
| `delete_orphans` | Remove files from destination that no longer exist in source |
| `backup_before_sync` | Copy destination to a timestamped backup before syncing |

---

## 🤖 Android / Termux usage

`dirsync` works natively in Termux. A typical workflow for syncing an Acode project to Termux:

```bash
python dirsync.py \
  --source ~/storage/shared/MyProject \
  --dest ~/MyProject \
  --auto
```

Or use watch mode while you edit in Acode:

```bash
python dirsync.py --watch 10 --auto
```

---

## 🤝 Contributing

Pull requests are welcome. For major changes, please open an issue first.

---

## 📜 License

MIT © [FuegoDev](https://github.com/fuegodev369)

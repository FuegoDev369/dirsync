#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dirsync.py
----------
A zero-dependency, interactive file synchronizer for any two directories.

Designed to work on any platform: Linux, macOS, Windows, Android (Termux).

Sync directions:
    src → dst   Source to Destination        (default)
    dst → src   Destination to Source        (reverse)
    smart       Bidirectional — newest file wins

Selection modes:
    all         Apply all detected changes    (default)
    --pick      Choose files one by one
    --ext       Filter by file extension(s)

Usage:
    python dirsync.py                          # interactive mode
    python dirsync.py --dry-run                # simulate, no changes made
    python dirsync.py --auto                   # sync without confirmation prompt
    python dirsync.py --direction smart        # bidirectional sync
    python dirsync.py --direction dst          # reverse sync
    python dirsync.py --pick                   # manual file selection
    python dirsync.py --ext jsx css js         # filter by extension(s)
    python dirsync.py --watch 5                # watch mode, sync every 5 seconds
    python dirsync.py --log                    # save sync history to a log file
    python dirsync.py --source /a --dest /b    # override paths on the fly
    python dirsync.py --no-color               # disable color output (pipes, CI)
    python dirsync.py --config                 # open settings menu
    python dirsync.py --version
    python dirsync.py --help
"""

__version__ = "1.1.0"
__author__  = "FuegoDev"
__license__ = "MIT"

import os
import sys
import json
import stat
import copy
import shutil
import atexit
import fnmatch
import hashlib
import argparse
import tempfile
import time
from pathlib import Path
from datetime import datetime


# ─────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent

# Android's shared storage (/storage/emulated/0/, /sdcard/) uses a FUSE
# layer that can silently drop writes or hide files on the next read.
# Termux's private home directory is always reliable. When the script
# lives in shared storage, we store config and log in home instead.
_SHARED_ROOTS = ("/storage/", "/sdcard/", "/mnt/sdcard/")
_in_shared    = any(str(SCRIPT_DIR).startswith(r) for r in _SHARED_ROOTS)
CONFIG_DIR    = Path.home() if _in_shared else SCRIPT_DIR
CONFIG_FILE   = CONFIG_DIR / ".dirsync_config.json"
LOG_FILE      = CONFIG_DIR / ".dirsync.log"
LOCK_FILE     = CONFIG_DIR / ".dirsync.lock"

DEFAULT_CONFIG = {
    "source":             "",
    "destination":        "",
    "ignore_patterns":    [
        "node_modules", ".git", ".vite", "dist", "build",
        ".cache", "__pycache__", "*.log", "package-lock.json",
        ".DS_Store", "Thumbs.db",
    ],
    "delete_orphans":     False,
    "backup_before_sync": False,
}

# Populated in main() based on TTY detection and --no-color flag.
# All cprint() calls read this; nothing else should touch it directly.
_USE_COLOR = True


# ─────────────────────────────────────────────────────────────────
#  TERMINAL COLORS
# ─────────────────────────────────────────────────────────────────
class Colors:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    CYAN    = "\033[96m"
    MAGENTA = "\033[95m"
    BLUE    = "\033[94m"
    GREY    = "\033[90m"
    WHITE   = "\033[97m"


def cprint(msg, color="", bold=False):
    if _USE_COLOR:
        prefix = Colors.BOLD if bold else ""
        print(f"{prefix}{color}{msg}{Colors.RESET}")
    else:
        print(msg)


def print_rule(color=Colors.GREY):
    cprint("─" * 52, color)


def print_section(label, color=Colors.CYAN):
    cprint(f"── {label} ──", color, bold=True)


def banner():
    inner    = f"dirsync  v{__version__}  by FuegoDev"
    subtitle = "Two-directory file synchronizer"
    width    = max(len(inner), len(subtitle)) + 8
    print()
    cprint("╔" + "═" * width + "╗", Colors.CYAN, bold=True)
    cprint("║" + inner.center(width)    + "║", Colors.CYAN, bold=True)
    cprint("║" + subtitle.center(width) + "║", Colors.CYAN, bold=True)
    cprint("╚" + "═" * width + "╝", Colors.CYAN, bold=True)
    print()


# ─────────────────────────────────────────────────────────────────
#  EXCEPTIONS
# ─────────────────────────────────────────────────────────────────
class UserCancelled(Exception):
    """Raised anywhere the user intentionally aborts an operation."""


# ─────────────────────────────────────────────────────────────────
#  LOCK FILE
# ─────────────────────────────────────────────────────────────────
def _pid_alive(pid):
    """Return True if the given PID is a running process."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        # ProcessLookupError (no such process) or PermissionError (exists but
        # we can't signal it) both resolve the same way for our purposes.
        return False


def acquire_lock():
    """
    Create a PID lock file to prevent concurrent runs against the same config.
    Returns True if acquired, False if another live instance is already running.
    """
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if _pid_alive(pid):
                return False
        except (ValueError, IOError):
            pass  # stale or corrupt — overwrite it
    try:
        LOCK_FILE.write_text(str(os.getpid()))
        atexit.register(_release_lock)
    except IOError:
        pass  # lock is best-effort; don't abort a sync over a write failure
    return True


def _release_lock():
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────
#  CONFIG — LOAD / SAVE / DISPLAY
# ─────────────────────────────────────────────────────────────────
def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            # Backfill any keys added in newer versions of the script.
            # deepcopy prevents sharing mutable defaults (e.g. the patterns list)
            # between the loaded config and DEFAULT_CONFIG.
            for key, val in DEFAULT_CONFIG.items():
                config.setdefault(key, copy.deepcopy(val))
            return config
        except (json.JSONDecodeError, IOError) as e:
            cprint(f"[!] Corrupted config ({e}), using defaults.", Colors.YELLOW)
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config):
    """
    Write config atomically: write to a sibling temp file, then rename.
    This prevents a half-written config if the process is killed mid-save.
    Permissions are set to 0o600 — config contains file paths that other
    users on the same machine don't need to read.
    """
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".dirsync_tmp_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_FILE)
        try:
            os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # best-effort; not fatal on Windows or restricted environments
        cprint(f"[✓] Config saved → {CONFIG_FILE}", Colors.GREEN)
    except IOError as e:
        cprint(f"[✗] Could not save config: {e}", Colors.RED)
        cprint("[!] Make sure the script directory is writable.", Colors.YELLOW)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def show_config(config):
    print_section("Current configuration")
    src = config["source"]      or "(not set)"
    dst = config["destination"] or "(not set)"
    cprint(f"  Source          : {src}", Colors.BLUE)
    cprint(f"  Destination     : {dst}", Colors.BLUE)
    cprint(f"  Ignored         : {', '.join(config['ignore_patterns'])}", Colors.GREY)
    cprint(f"  Delete orphans  : {'Yes' if config['delete_orphans'] else 'No'}", Colors.GREY)
    cprint(f"  Auto backup     : {'Yes' if config['backup_before_sync'] else 'No'}", Colors.GREY)
    print()


def run_config_menu(config):
    """
    Interactive settings menu. Loops until the user presses 0 or Ctrl+C.
    Saves only when at least one value was actually changed.
    """
    changed = False

    while True:
        show_config(config)
        cprint("What would you like to change?", Colors.YELLOW, bold=True)
        print("  [1] Source path")
        print("  [2] Destination path")
        print("  [3] Delete orphan files")
        print("  [4] Enable auto-backup before sync")
        print("  [5] Manage ignored patterns")
        print("  [0] Back")
        print()

        try:
            choice = input("Your choice: ").strip()
        except KeyboardInterrupt:
            print()
            break

        if choice == "0":
            break

        try:
            if choice == "1":
                v = input(f"New source path [{config['source']}]: ").strip()
                if v:
                    config["source"] = os.path.realpath(os.path.expanduser(v))
                    changed = True
            elif choice == "2":
                v = input(f"New destination path [{config['destination']}]: ").strip()
                if v:
                    config["destination"] = os.path.realpath(os.path.expanduser(v))
                    changed = True
            elif choice == "3":
                cur = "Yes" if config["delete_orphans"] else "No"
                v = input(f"Delete orphan files? (y/n) [{cur}]: ").strip().lower()
                if v in ("y", "yes"):
                    config["delete_orphans"] = True
                    changed = True
                elif v in ("n", "no"):
                    config["delete_orphans"] = False
                    changed = True
            elif choice == "4":
                cur = "Yes" if config["backup_before_sync"] else "No"
                v = input(f"Enable auto-backup? (y/n) [{cur}]: ").strip().lower()
                if v in ("y", "yes"):
                    config["backup_before_sync"] = True
                    changed = True
                elif v in ("n", "no"):
                    config["backup_before_sync"] = False
                    changed = True
            elif choice == "5":
                print("\nCurrent patterns:", ", ".join(config["ignore_patterns"]))
                print("  [a] Add a pattern")
                print("  [r] Remove a pattern")
                try:
                    sub = input("Choice: ").strip().lower()
                except KeyboardInterrupt:
                    print()
                    continue
                if sub == "a":
                    p = input("Pattern to add (e.g. *.tmp, test_*): ").strip()
                    if p and p not in config["ignore_patterns"]:
                        config["ignore_patterns"].append(p)
                        cprint(f"[+] '{p}' added.", Colors.GREEN)
                        changed = True
                elif sub == "r":
                    p = input("Pattern to remove: ").strip()
                    if p in config["ignore_patterns"]:
                        config["ignore_patterns"].remove(p)
                        cprint(f"[-] '{p}' removed.", Colors.YELLOW)
                        changed = True
                    else:
                        cprint("[?] Pattern not found.", Colors.GREY)
        except KeyboardInterrupt:
            print()
            continue

    if changed:
        save_config(config)
    else:
        cprint("[i] No changes made.", Colors.GREY)


# ─────────────────────────────────────────────────────────────────
#  FIRST-RUN WIZARD
# ─────────────────────────────────────────────────────────────────
def _prompt_path(label):
    """Ask for a directory path, re-prompting until a non-empty value is given."""
    while True:
        try:
            value = input(f"  {label}: ").strip()
        except KeyboardInterrupt:
            print()
            raise UserCancelled("Setup interrupted. No config saved.")
        if value:
            return os.path.realpath(os.path.expanduser(value))
        cprint("  [!] This field is required — please enter a path.", Colors.YELLOW)


def first_run_wizard(config):
    """
    Guide the user through setting source and destination paths.
    Both fields are mandatory — the wizard re-prompts until filled.
    Only saves config once both paths are provided.
    """
    cprint("Let's configure your sync paths.", Colors.CYAN, bold=True)
    cprint(f"(Config will be saved to: {CONFIG_FILE})", Colors.GREY)
    print()

    if not config["source"]:
        config["source"] = _prompt_path("Source directory path     ")
    if not config["destination"]:
        config["destination"] = _prompt_path("Destination directory path")

    save_config(config)
    print()
    return config


# ─────────────────────────────────────────────────────────────────
#  FILE UTILITIES
# ─────────────────────────────────────────────────────────────────
def file_hash(filepath):
    """Return the MD5 hex digest of a file's content, or None on any read error."""
    h = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except (IOError, PermissionError):
        return None


def should_ignore(path_str, ignore_patterns):
    """
    Return True if any component of the path matches a glob ignore pattern.

    Uses fnmatch so the full glob syntax works: *.log, test_*, file?.txt,
    [abc]*.py, etc. Matching is applied to each individual path component
    (directory names and the filename), not the full path string.
    """
    parts = Path(path_str).parts
    return any(
        fnmatch.fnmatch(part, pattern)
        for part in parts
        for pattern in ignore_patterns
    )


def _safe_walk(root):
    """
    Recursively yield regular files under root.
    Symlinks are skipped to avoid following loops or unintended targets.
    Directories that raise PermissionError are skipped silently rather
    than aborting the entire scan.
    """
    try:
        for entry in root.iterdir():
            if entry.is_symlink():
                continue
            if entry.is_dir():
                yield from _safe_walk(entry)
            elif entry.is_file():
                yield entry
    except PermissionError:
        pass


def _files_differ(a, b):
    """
    Return True if two file entries represent different content.

    Size is checked first (O(1), no I/O). Hash is computed lazily and
    cached in the entry dict, so each file is read at most once per scan.

    If either file is unreadable (hash returns None), the pair is treated
    as different rather than silently skipping a potential change.
    """
    if a["size"] != b["size"]:
        return True

    if "hash" not in a:
        a["hash"] = file_hash(a["full"])
    if "hash" not in b:
        b["hash"] = file_hash(b["full"])

    ha, hb = a["hash"], b["hash"]
    if ha is None or hb is None:
        # At least one file is unreadable — flag as different so the user
        # sees an error during apply rather than a silent skip.
        return True
    return ha != hb


def collect_files(root_dir, ignore_patterns, ext_filter=None):
    """
    Walk root_dir and return a dict keyed by POSIX-style relative path:
        { "sub/dir/file.txt": { size, mtime_raw, mtime, full } }

    Using .as_posix() for keys guarantees forward slashes on all platforms,
    so the same key works for both sides of a comparison on Windows.

    Hash values are NOT computed here — they are populated on demand by
    _files_differ() to avoid reading files that haven't changed.
    ext_filter: list of lowercase extensions to keep (e.g. ['.jsx', '.css']).
    """
    root  = Path(root_dir)
    files = {}
    if not root.exists():
        return files

    for fpath in _safe_walk(root):
        rel = fpath.relative_to(root).as_posix()
        if should_ignore(rel, ignore_patterns):
            continue
        if ext_filter and fpath.suffix.lower() not in ext_filter:
            continue
        try:
            st = fpath.stat()
        except OSError:
            continue
        files[rel] = {
            "size":      st.st_size,
            "mtime_raw": st.st_mtime,
            "mtime":     datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d  %H:%M:%S"),
            "full":      str(fpath),
        }
    return files


# ─────────────────────────────────────────────────────────────────
#  CHANGE DETECTION
# ─────────────────────────────────────────────────────────────────
def detect_changes(src_files, dst_files, direction="src"):
    """
    Compare src_files and dst_files according to the sync direction.

    direction:
        'src'   → source is authoritative  (src → dst)
        'dst'   → destination is authoritative (dst → src)
        'smart' → newest file on either side wins (bidirectional)

    Returns:
        to_copy   : dict of files to copy, keyed by relative path
        to_delete : sorted list of relative paths to remove
    """
    src_set   = set(src_files.keys())
    dst_set   = set(dst_files.keys())
    common    = src_set & dst_set
    to_copy   = {}
    to_delete = []

    if direction == "src":
        for rel in sorted(src_set - dst_set):
            to_copy[rel] = {
                "from": src_files[rel]["full"],
                "tag":  "new",
                "info": src_files[rel],
            }
        for rel in sorted(common):
            if _files_differ(src_files[rel], dst_files[rel]):
                to_copy[rel] = {
                    "from":     src_files[rel]["full"],
                    "tag":      "modified",
                    "src_info": src_files[rel],
                    "dst_info": dst_files[rel],
                }
        to_delete = sorted(dst_set - src_set)

    elif direction == "dst":
        for rel in sorted(dst_set - src_set):
            to_copy[rel] = {
                "from": dst_files[rel]["full"],
                "tag":  "new",
                "info": dst_files[rel],
            }
        for rel in sorted(common):
            if _files_differ(src_files[rel], dst_files[rel]):
                to_copy[rel] = {
                    "from":     dst_files[rel]["full"],
                    "tag":      "modified",
                    "src_info": dst_files[rel],
                    "dst_info": src_files[rel],
                }
        to_delete = sorted(src_set - dst_set)

    elif direction == "smart":
        for rel in sorted(src_set - dst_set):
            to_copy[rel] = {
                "from":   src_files[rel]["full"],
                "tag":    "new (src)",
                "info":   src_files[rel],
                "winner": "source",
            }
        for rel in sorted(dst_set - src_set):
            to_copy[rel] = {
                "from":   dst_files[rel]["full"],
                "tag":    "new (dst)",
                "info":   dst_files[rel],
                "winner": "destination",
            }
        for rel in sorted(common):
            src_e = src_files[rel]
            dst_e = dst_files[rel]
            if not _files_differ(src_e, dst_e):
                continue
            if src_e["mtime_raw"] >= dst_e["mtime_raw"]:
                winner, frm, win_info, lose_info = "source",      src_e["full"], src_e, dst_e
            else:
                winner, frm, win_info, lose_info = "destination", dst_e["full"], dst_e, src_e
            to_copy[rel] = {
                "from":      frm,
                "tag":       "modified",
                "winner":    winner,
                "win_info":  win_info,
                "lose_info": lose_info,
            }

    return to_copy, to_delete


# ─────────────────────────────────────────────────────────────────
#  CHANGE REPORT
# ─────────────────────────────────────────────────────────────────
def print_report(to_copy, to_delete, direction, delete_orphans, src_files, dst_files):
    """Print a detailed change report. Returns True if actionable changes exist."""
    total = len(to_copy) + (len(to_delete) if delete_orphans else 0)

    dir_labels = {
        "src":   "Source  ➜  Destination",
        "dst":   "Destination  ➜  Source",
        "smart": "Bidirectional smart (newest wins)",
    }
    cprint(f"  Mode: {dir_labels.get(direction, direction)}", Colors.MAGENTA)
    print()

    if not to_copy and not to_delete:
        cprint("✓ No changes — both directories are identical.", Colors.GREEN, bold=True)
        return False

    new_files = {r: v for r, v in to_copy.items() if "new" in v["tag"]}
    modified  = {r: v for r, v in to_copy.items() if v["tag"] == "modified"}

    print_section(f"{total} change(s) detected", Colors.YELLOW)
    print()

    if new_files:
        cprint(f"  [+] {len(new_files)} new file(s):", Colors.GREEN, bold=True)
        for rel, v in new_files.items():
            info  = v.get("info", {})
            label = f"  [{v['winner']}]" if "winner" in v else ""
            cprint(f"       + {rel}{label}", Colors.GREEN)
            cprint(f"         Added on : {info.get('mtime', '?')}", Colors.GREY)
        print()

    if modified:
        cprint(f"  [~] {len(modified)} modified file(s):", Colors.YELLOW, bold=True)
        for rel, v in modified.items():
            if direction == "smart":
                win_info  = v.get("win_info",  {})
                lose_info = v.get("lose_info", {})
                loser     = "source" if v["winner"] == "destination" else "destination"
                cprint(f"       ~ {rel}", Colors.YELLOW)
                cprint(f"         ✓ {v['winner']:11s} (kept)    : {win_info.get('mtime', '?')}", Colors.GREEN)
                cprint(f"         ✗ {loser:11s} (skipped) : {lose_info.get('mtime', '?')}", Colors.GREY)
            else:
                src_info = v.get("src_info", {})
                dst_info = v.get("dst_info", {})
                cprint(f"       ~ {rel}", Colors.YELLOW)
                cprint(f"         Source      modified : {src_info.get('mtime', '?')}", Colors.GREY)
                cprint(f"         Destination modified : {dst_info.get('mtime', '?')}", Colors.GREY)
        print()

    if to_delete:
        ref = dst_files if direction in ("src", "smart") else src_files
        if delete_orphans:
            cprint(f"  [-] {len(to_delete)} orphan file(s) to delete:", Colors.RED, bold=True)
        else:
            cprint(f"  [?] {len(to_delete)} orphan file(s) (deletion disabled):", Colors.GREY, bold=True)
        for rel in to_delete:
            info  = ref.get(rel, {})
            color = Colors.RED if delete_orphans else Colors.GREY
            mark  = "-" if delete_orphans else "?"
            cprint(f"       {mark} {rel}", color)
            cprint(f"         Last modified : {info.get('mtime', '?')}", Colors.GREY)
        print()

    return True


# ─────────────────────────────────────────────────────────────────
#  MANUAL FILE SELECTION  (--pick)
# ─────────────────────────────────────────────────────────────────
def pick_files(to_copy, to_delete):
    """
    Let the user choose exactly which files to include in the sync.
    Returns filtered (to_copy, to_delete).
    Raises UserCancelled if the user explicitly aborts.
    """
    items = [("copy", rel, v) for rel, v in to_copy.items()]
    items += [("delete", rel, None) for rel in to_delete]

    if not items:
        return to_copy, to_delete

    print_section("Manual file selection", Colors.CYAN)
    cprint("  Enter numbers to include (e.g. 1 3 5), 'all', or '0' to cancel.", Colors.WHITE)
    print()

    for i, (action, rel, v) in enumerate(items, 1):
        if action == "copy":
            tag   = v.get("tag", "?")
            sym   = "+" if "new" in tag else "~"
            color = Colors.GREEN if "new" in tag else Colors.YELLOW
            cprint(f"  [{i:2d}] {sym} {rel}", color)
        else:
            cprint(f"  [{i:2d}] - {rel}  (orphan)", Colors.RED)

    print()
    try:
        raw = input("Selected numbers (or 'all'): ").strip().lower()
    except KeyboardInterrupt:
        print()
        raise UserCancelled("Selection cancelled.")

    if raw == "0":
        raise UserCancelled("Selection cancelled.")

    if raw in ("all", "a"):
        return to_copy, to_delete

    try:
        indices = {int(x) for x in raw.split()}
    except ValueError:
        cprint("[!] Invalid input — all files will be processed.", Colors.YELLOW)
        return to_copy, to_delete

    selected_copy   = {}
    selected_delete = []
    for i, (action, rel, v) in enumerate(items, 1):
        if i not in indices:
            continue
        if action == "copy":
            selected_copy[rel] = v
        else:
            selected_delete.append(rel)

    cprint(f"\n[✓] {len(selected_copy) + len(selected_delete)} file(s) selected.", Colors.CYAN)
    print()
    return selected_copy, selected_delete


# ─────────────────────────────────────────────────────────────────
#  BACKUP
# ─────────────────────────────────────────────────────────────────
def create_backup(path):
    """
    Copy the target directory to a timestamped backup folder.
    Microseconds are included in the timestamp to handle rapid successive
    calls (e.g. watch mode with a 1-second interval).
    """
    p = Path(path)
    if not p.exists():
        return None
    ts     = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = p.parent / f"{p.name}_backup_{ts}"
    try:
        shutil.copytree(str(p), str(backup))
        cprint(f"[✓] Backup created: {backup}", Colors.CYAN)
        return backup
    except Exception as e:
        cprint(f"[✗] Backup failed: {e}", Colors.RED)
        return None


# ─────────────────────────────────────────────────────────────────
#  SYNC ENGINE
# ─────────────────────────────────────────────────────────────────
def _copy_atomic(src, dst):
    """
    Copy src to dst atomically.
    Writes into a sibling temp file, then renames it into place via
    os.replace(). This means a partial file never appears at the
    final destination path, even if the process is interrupted mid-copy.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dst.parent)
    try:
        os.close(fd)
        shutil.copy2(str(src), tmp_path)
        os.replace(tmp_path, str(dst))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def apply_sync(source, destination, to_copy, to_delete,
               direction="src", delete_orphans=False, dry_run=False):
    """
    Apply the selected file changes.
    Returns (success_count, error_count).
    """
    src_root = Path(source)
    dst_root = Path(destination)
    label    = "[DRY-RUN] " if dry_run else ""
    success  = errors = 0

    for rel, v in to_copy.items():
        frm   = Path(v["from"])
        tag   = v.get("tag", "modified")
        sym   = "+" if "new" in tag else "~"
        color = Colors.GREEN if sym == "+" else Colors.YELLOW

        if direction == "src":
            dst_file = dst_root / rel
        elif direction == "dst":
            dst_file = src_root / rel
        elif direction == "smart":
            winner   = v.get("winner", "source")
            dst_file = dst_root / rel if winner == "source" else src_root / rel
        else:
            dst_file = dst_root / rel

        cprint(f"  {label}[{sym}] {rel}", color)

        if not dry_run:
            try:
                _copy_atomic(frm, dst_file)
                success += 1
            except Exception as e:
                cprint(f"       [✗] Error: {e}", Colors.RED)
                errors += 1
        else:
            success += 1

    if delete_orphans:
        ref_root = dst_root if direction in ("src", "smart") else src_root
        for rel in to_delete:
            target = ref_root / rel
            cprint(f"  {label}[-] {rel}", Colors.RED)
            if not dry_run:
                try:
                    target.unlink()
                    success += 1
                except Exception as e:
                    cprint(f"       [✗] Delete error: {e}", Colors.RED)
                    errors += 1
            else:
                success += 1

    return success, errors


# ─────────────────────────────────────────────────────────────────
#  LOG WRITER
# ─────────────────────────────────────────────────────────────────
def write_log(source, destination, direction, success, errors, copied, deleted):
    """Append a sync summary entry to the log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"\n[{ts}]",
        f"  source      : {source}",
        f"  destination : {destination}",
        f"  direction   : {direction}",
        f"  copied      : {copied}",
        f"  deleted     : {deleted}",
        f"  success     : {success}",
        f"  errors      : {errors}",
    ]
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        cprint(f"[✓] Log updated → {LOG_FILE}", Colors.GREY)
    except IOError as e:
        cprint(f"[!] Could not write log: {e}", Colors.YELLOW)


# ─────────────────────────────────────────────────────────────────
#  SINGLE SYNC PASS
# ─────────────────────────────────────────────────────────────────
def run_sync(config, args):
    """
    Execute one full sync pass.
    Returns (success, errors, copied_count, deleted_count).
    Raises UserCancelled if the user aborts interactively.
    """
    source      = os.path.expanduser(config["source"])
    destination = os.path.expanduser(config["destination"])
    ignore      = config["ignore_patterns"]

    ext_filter = None
    if args.ext:
        ext_filter = [e if e.startswith(".") else f".{e}" for e in args.ext]
        cprint(f"[i] Extension filter active: {', '.join(ext_filter)}", Colors.MAGENTA)
        print()

    direction = args.direction or "src"

    # Verify paths exist.
    # In --auto mode, missing directories are a hard error — we cannot prompt.
    for label_str, p in [("Source", source), ("Destination", destination)]:
        if not Path(p).exists():
            cprint(f"[?] {label_str} not found: {p}", Colors.YELLOW)
            if args.auto:
                cprint("[✗] Cannot prompt for directory creation in --auto mode.", Colors.RED)
                return 0, 1, 0, 0
            try:
                rep = input(f"    Create this directory? (y/n): ").strip().lower()
            except KeyboardInterrupt:
                print()
                raise UserCancelled("Cancelled.")
            if rep in ("y", "yes"):
                try:
                    Path(p).mkdir(parents=True, exist_ok=True)
                    cprint("[✓] Directory created.", Colors.GREEN)
                except Exception as e:
                    cprint(f"[✗] Could not create directory: {e}", Colors.RED)
                    return 0, 1, 0, 0
            else:
                raise UserCancelled("Cancelled.")

    # Scan both sides
    cprint("Scanning files...", Colors.CYAN)
    src_files = collect_files(source,      ignore, ext_filter)
    dst_files = collect_files(destination, ignore, ext_filter)
    cprint(f"  Source      : {len(src_files)} file(s)", Colors.GREY)
    cprint(f"  Destination : {len(dst_files)} file(s)", Colors.GREY)
    print()

    # Safety guard for watch mode with delete_orphans enabled:
    # If the source scan came back empty but the destination has files,
    # it likely means the source directory became unavailable (unmounted
    # drive, network share, etc.). Proceeding would delete everything in
    # the destination. Abort instead.
    if (not src_files and dst_files
            and config["delete_orphans"]
            and direction in ("src", "smart")):
        cprint("[!] Source appears empty but destination has files.", Colors.YELLOW)
        cprint("[!] Refusing to delete destination files from an empty source scan.", Colors.YELLOW)
        cprint("[!] Verify that the source directory is accessible.", Colors.RED)
        return 0, 1, 0, 0

    # Detect and display changes
    to_copy, to_delete = detect_changes(src_files, dst_files, direction)

    has_changes = print_report(
        to_copy, to_delete, direction,
        config["delete_orphans"], src_files, dst_files,
    )

    if not has_changes:
        return 0, 0, 0, 0

    # Manual file selection
    if args.pick:
        to_copy, to_delete = pick_files(to_copy, to_delete)
        if not to_copy and not to_delete:
            cprint("No files selected. Cancelled.", Colors.GREY)
            return 0, 0, 0, 0

    # Dry-run simulation
    if args.dry_run:
        print_section("DRY-RUN mode (simulation — no files modified)", Colors.MAGENTA)
        apply_sync(source, destination, to_copy, to_delete,
                   direction, config["delete_orphans"], dry_run=True)
        print()
        cprint("[i] No files were modified.", Colors.MAGENTA)
        return 0, 0, 0, 0

    # Confirmation prompt
    if not args.auto:
        cprint("Apply these changes? (y/n): ", Colors.YELLOW, bold=True)
        try:
            rep = input("➜ ").strip().lower()
        except KeyboardInterrupt:
            print()
            raise UserCancelled("Sync cancelled.")
        if rep not in ("y", "yes"):
            cprint("Sync cancelled.", Colors.GREY)
            return 0, 0, 0, 0
        print()

    # Backup
    if config["backup_before_sync"]:
        print_section("Creating backup...")
        target_backup = destination if direction in ("src", "smart") else source
        create_backup(target_backup)
        print()

    # Apply changes
    print_section("Syncing files...")
    success, errors = apply_sync(
        source, destination, to_copy, to_delete,
        direction, config["delete_orphans"], dry_run=False,
    )

    copied_count  = len(to_copy)
    deleted_count = len(to_delete) if config["delete_orphans"] else 0

    # Summary
    print()
    print_section("Summary")
    ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    if errors == 0:
        cprint(f"  ✓ {success} operation(s) completed — {ts}", Colors.GREEN, bold=True)
    else:
        cprint(f"  ~ {success} succeeded, {errors} error(s) — {ts}", Colors.YELLOW, bold=True)
    print()

    return success, errors, copied_count, deleted_count


# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────
def main():
    global _USE_COLOR

    parser = argparse.ArgumentParser(
        prog="dirsync",
        description="dirsync — zero-dependency two-directory file synchronizer",
    )
    parser.add_argument("--dry-run",   action="store_true",
                        help="Simulate sync — no files are modified")
    parser.add_argument("--auto",      action="store_true",
                        help="Skip confirmation prompt (non-interactive)")
    parser.add_argument("--config",    action="store_true",
                        help="Open the settings menu")
    parser.add_argument("--direction", choices=["src", "dst", "smart"], default=None,
                        help="src=Source→Dest (default), dst=Dest→Source, smart=bidirectional")
    parser.add_argument("--pick",      action="store_true",
                        help="Manually select files to sync (interactive; cannot combine with --auto or --watch)")
    parser.add_argument("--ext",       nargs="+", default=None,
                        help="Filter by file extension(s): --ext jsx css js")
    parser.add_argument("--watch",     type=int, default=None, metavar="SECONDS",
                        help="Watch mode: repeat sync every N seconds (N >= 1)")
    parser.add_argument("--log",       action="store_true",
                        help=f"Append sync history to {LOG_FILE}")
    parser.add_argument("--source",    type=str,
                        help="Override source path for this run only (not saved to config)")
    parser.add_argument("--dest",      type=str,
                        help="Override destination path for this run only (not saved to config)")
    parser.add_argument("--no-color",  action="store_true",
                        help="Disable ANSI color output (useful for pipes and CI systems)")
    parser.add_argument("--version",   action="version",
                        version=f"dirsync v{__version__}")
    args = parser.parse_args()

    # Color: disable on explicit flag, non-TTY stdout, or the NO_COLOR env convention.
    if args.no_color or not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        _USE_COLOR = False

    # Mutually exclusive combinations that argparse can't express natively.
    if args.pick and args.auto:
        parser.error("--pick requires interactive input and cannot be combined with --auto")
    if args.pick and args.watch:
        parser.error("--pick requires interactive input and cannot be combined with --watch")
    if args.watch is not None and args.watch < 1:
        parser.error("--watch interval must be at least 1 second")

    banner()

    config = load_config()

    # Settings menu (loops internally until the user exits)
    if args.config:
        run_config_menu(config)
        return

    # Session-only path overrides — not persisted to config on disk
    if args.source:
        config["source"] = os.path.realpath(os.path.expanduser(args.source))
    if args.dest:
        config["destination"] = os.path.realpath(os.path.expanduser(args.dest))

    # First-run wizard if paths are missing
    if not config["source"] or not config["destination"]:
        config = first_run_wizard(config)

    show_config(config)

    # Prevent two instances from running against the same config simultaneously
    if not acquire_lock():
        cprint("[✗] Another dirsync instance is already running with this config.", Colors.RED)
        cprint("    Remove the lock file if this is wrong:", Colors.YELLOW)
        cprint(f"    {LOCK_FILE}", Colors.GREY)
        sys.exit(1)

    # Interactive direction prompt (skipped in --watch or --auto mode)
    if args.direction is None and not args.auto and not args.watch:
        print_section("Sync direction")
        print("  [1] Source  ➜  Destination   (default)")
        print("  [2] Destination  ➜  Source   (reverse)")
        print("  [3] Bidirectional smart       (newest wins)")
        print()
        try:
            raw = input("Direction (1/2/3) [1]: ").strip()
        except KeyboardInterrupt:
            print()
            cprint("\n[✗] Cancelled.", Colors.YELLOW)
            sys.exit(0)
        args.direction = {"2": "dst", "3": "smart"}.get(raw, "src")
        print()

    if args.direction is None:
        args.direction = "src"

    total_errors = 0

    # ── Watch mode ───────────────────────────────────────────────
    if args.watch:
        interval = args.watch
        cprint(f"[i] Watch mode: syncing every {interval}s — Ctrl+C to stop.", Colors.CYAN, bold=True)
        print()
        try:
            while True:
                cprint(f"\n── Sync @ {datetime.now().strftime('%H:%M:%S')} ──", Colors.CYAN)
                try:
                    success, errors, copied, deleted = run_sync(config, args)
                except UserCancelled as e:
                    cprint(f"[i] {e}", Colors.GREY)
                    break
                total_errors += errors
                if args.log:
                    write_log(
                        config["source"], config["destination"],
                        args.direction, success, errors, copied, deleted,
                    )
                time.sleep(interval)
        except KeyboardInterrupt:
            print()
            cprint("[i] Watch mode stopped.", Colors.GREY)
        sys.exit(1 if total_errors else 0)

    # ── Single pass ──────────────────────────────────────────────
    try:
        success, errors, copied, deleted = run_sync(config, args)
    except UserCancelled as e:
        cprint(f"[i] {e}", Colors.GREY)
        sys.exit(0)

    if args.log:
        write_log(
            config["source"], config["destination"],
            args.direction, success, errors, copied, deleted,
        )

    sys.exit(1 if errors else 0)


# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        cprint("\n[✗] Interrupted. Goodbye.", Colors.YELLOW)
        sys.exit(0)
    except UserCancelled as e:
        print()
        cprint(f"[✗] {e}", Colors.YELLOW)
        sys.exit(0)

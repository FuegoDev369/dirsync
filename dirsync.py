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
    python dirsync.py --backups                # browse and restore backups
    python dirsync.py --version
    python dirsync.py --help
"""

__version__ = "2.0.0"
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
from concurrent.futures import ThreadPoolExecutor


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
    "backup_mode":        "full",
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
    cprint(f"  Backup mode     : {config['backup_mode']}", Colors.GREY)
    print()


def _prompt_config_path(current, label):
    """
    Prompt for a new value for a path-typed config field (source/destination).

    Resolves the input via realpath/expanduser and checks existence. A
    nonexistent path is allowed but requires explicit confirmation (default
    no), since it may simply not have been created yet. Resolution failures
    (e.g. invalid characters, embedded NUL bytes) are caught and re-prompted
    instead of crashing the menu.

    Returns the resolved path string to assign, or None if the field should
    be left unchanged (empty input, or declined confirmation on a missing
    path) — both cases are treated identically, per spec.
    """
    while True:
        v = input(f"New {label} [{current}]: ").strip()
        if not v:
            return None
        try:
            resolved = os.path.realpath(os.path.expanduser(v))
        except (OSError, ValueError) as e:
            cprint(f"[✗] Invalid path: {e}", Colors.RED)
            continue
        if not Path(resolved).exists():
            cprint(f"[!] This path does not exist: {resolved}", Colors.YELLOW)
            confirm = input("Save it anyway? (y/n) [n]: ").strip().lower()
            if confirm not in ("y", "yes"):
                return None
        return resolved


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
        print("  [5] Backup mode (full / focus)")
        print("  [6] Manage ignored patterns")
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
                v = _prompt_config_path(config["source"], "source path")
                if v is not None:
                    config["source"] = v
                    changed = True
            elif choice == "2":
                v = _prompt_config_path(config["destination"], "destination path")
                if v is not None:
                    config["destination"] = v
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
                cprint(f"\nCurrent backup mode: {config['backup_mode']}", Colors.GREY)
                print("  [1] full")
                print("  [2] focus")
                print("  [0] Cancel")
                try:
                    sub = input("Choice: ").strip()
                except KeyboardInterrupt:
                    print()
                    continue
                if sub == "1":
                    config["backup_mode"] = "full"
                    changed = True
                    cprint("[✓] Backup mode set to 'full'.", Colors.GREEN)
                elif sub == "2":
                    config["backup_mode"] = "focus"
                    changed = True
                    cprint("[✓] Backup mode set to 'focus'.", Colors.GREEN)
                elif sub == "0":
                    pass
                else:
                    cprint("[?] Invalid choice.", Colors.GREY)
            elif choice == "6":
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


def _scan_tree(dir_path, ignore_patterns, rel_prefix=""):
    """
    Recursively yield (full_path, stat_result, rel) for every regular file
    under dir_path, using os.scandir instead of Path.iterdir() + separate
    is_*()/stat() calls.

    Each os.DirEntry caches type information from the underlying readdir()
    call (d_type on most POSIX systems, a native cache on Windows), so
    is_symlink()/is_dir()/is_file() are answered without a fresh stat() per
    property. The single entry.stat() taken here for regular files is the
    same stat() reused by the caller for size/mtime — never a second,
    redundant stat() call.

    Symlinks are skipped entirely (strict, unchanged behavior) before any
    stat() is attempted, so a broken symlink or one pointing at a directory
    never reaches stat(). Directories that raise PermissionError on
    os.scandir() are skipped silently rather than aborting the whole scan.

    Directory pruning: a directory is not descended into if its own
    relative path already matches ignore_patterns (via should_ignore(),
    same fnmatch-per-component logic used for files). This is a pure
    speed optimization, not a behavior change — should_ignore() checks
    EVERY component of a path, so any file underneath a matched directory
    would already have that matched component in its own path and would
    have been filtered out downstream anyway. Pruning just skips the
    os.scandir() work of walking into directories like node_modules,
    .git, dist, build, __pycache__, .cache before throwing their
    contents away.
    """
    try:
        with os.scandir(dir_path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    rel = f"{rel_prefix}/{entry.name}" if rel_prefix else entry.name
                    if entry.is_dir():
                        if should_ignore(rel, ignore_patterns):
                            continue  # pruned — no file below can escape this filter anyway
                        yield from _scan_tree(entry.path, ignore_patterns, rel)
                    elif entry.is_file():
                        try:
                            st = entry.stat()
                        except OSError:
                            # Race: file vanished between scandir() and stat().
                            continue
                        yield entry.path, st, rel
                except OSError:
                    # Race: entry vanished before its type could be queried.
                    continue
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

    _scan_tree() already prunes ignored directories during the walk, but
    should_ignore() is still applied here to each file's own relative
    path — a file's own name can match a pattern (*.log,
    package-lock.json, .DS_Store, ...) independently of any directory
    it lives in, and that check isn't covered by directory pruning.
    """
    root  = Path(root_dir)
    files = {}
    if not root.exists():
        return files

    for full_path, st, rel in _scan_tree(str(root), ignore_patterns):
        if should_ignore(rel, ignore_patterns):
            continue
        if ext_filter and Path(full_path).suffix.lower() not in ext_filter:
            continue
        files[rel] = {
            "size":      st.st_size,
            "mtime_raw": st.st_mtime,
            "mtime":     datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d  %H:%M:%S"),
            "full":      full_path,
        }
    return files


# ─────────────────────────────────────────────────────────────────
#  CHANGE DETECTION
# ─────────────────────────────────────────────────────────────────
def _max_hash_workers():
    """
    Bound the hashing thread pool.

    Termux/Android often runs on constrained hardware, over shared
    storage with FUSE overhead — cap harder there via TERMUX_VERSION
    detection. Elsewhere, hashing is I/O-bound (disk reads) and
    hashlib's OpenSSL backend releases the GIL on large chunks, so
    real parallelism is achieved with threads; oversubscribing cores
    (2x) helps hide read latency, capped at 16 to avoid exhausting
    file descriptors on very large trees.
    """
    is_termux = bool(os.environ.get("TERMUX_VERSION"))
    base = os.cpu_count() or 2
    return 4 if is_termux else min(16, base * 2)


def _prehash_candidates(common, src_files, dst_files):
    """
    Warm the per-entry hash cache for every same-size candidate pair in
    `common`, in parallel, before the sequential comparison passes run.

    Pass 1 (no I/O): a pair whose sizes already differ is already known
    to be "different" — no hash is needed, so it's skipped here
    entirely. This preserves the existing lazy-hashing rule: only
    same-size pairs ever get hashed.

    Pass 2 (bounded parallel I/O): every entry that still needs a hash
    is collected into a dict keyed by id(entry) — using the entry
    object's identity (not its path) guarantees each physical file is
    submitted to the pool at most once, even if it were reachable from
    more than one candidate pair. Each task calls the existing
    file_hash(), which already returns None on any read error instead
    of raising, so no exception can escape a task.

    After this returns, _files_differ() runs normally in sequence and
    always finds a warm cache (entry["hash"] already set) — its
    behavior, its return value, and the caller-visible output of
    detect_changes() are all unchanged. Only *when* each hash gets
    computed has changed, not *whether* or *how many times*.
    """
    candidates = {}
    for rel in common:
        s, d = src_files[rel], dst_files[rel]
        if s["size"] != d["size"]:
            continue  # size alone already proves "different" — no hash needed
        candidates[id(s)] = s
        candidates[id(d)] = d

    if not candidates:
        return  # nothing to hash — pool is never created, zero overhead

    def _hash_entry(entry):
        entry["hash"] = file_hash(entry["full"])

    with ThreadPoolExecutor(max_workers=_max_hash_workers()) as ex:
        # list() forces full consumption so the `with` block's clean
        # shutdown/join happens only after every task has completed —
        # including on Ctrl+C, where the context manager still waits
        # for in-flight tasks before the KeyboardInterrupt propagates.
        list(ex.map(_hash_entry, candidates.values()))


def detect_changes(src_files, dst_files, direction="src"):
    """
    Compare src_files and dst_files according to the sync direction.

    direction:
        'src'   → source is authoritative  (src → dst)
        'dst'   → destination is authoritative (dst → src)
        'smart' → newest file on either side wins (bidirectional)

    Before the per-direction comparison below, _prehash_candidates()
    pre-populates the hash cache for every common, same-size pair in a
    bounded thread pool. This is a pure speed optimization: the
    sequential _files_differ() calls that follow are unchanged and
    simply find the cache already warm, so the returned to_copy /
    to_delete are identical to the fully-sequential implementation.

    Returns:
        to_copy   : dict of files to copy, keyed by relative path
        to_delete : sorted list of relative paths to remove
    """
    src_set   = set(src_files.keys())
    dst_set   = set(dst_files.keys())
    common    = src_set & dst_set
    to_copy   = {}
    to_delete = []

    _prehash_candidates(common, src_files, dst_files)

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

# Characters invalid in Windows filenames — also stripped on other
# platforms for a single, predictable naming convention everywhere.
_INVALID_NAME_CHARS = '<>:"/\\|?*'


def backup_root_dir():
    """
    Centralized root for all backups (full and, later, focus), placed
    under CONFIG_DIR rather than as a sibling of the backed-up target.

    CONFIG_DIR already carries the Termux/shared-storage detection
    (_in_shared) used for the config and lock files, so centralizing
    backups here makes them Termux-safe for free, with no new FUSE
    detection logic to write or keep in sync.
    """
    root = CONFIG_DIR / "dirsync_backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _sanitize_project_name(name):
    """Replace characters invalid in Windows filenames with '_'."""
    for ch in _INVALID_NAME_CHARS:
        name = name.replace(ch, "_")
    return name


def _backup_name(target_path, mode, direction):
    """
    Build a backup folder name following the shared full/focus convention:
        {project}_{mode}_{direction}_{YYYYMMDD_HHMMSS_ffffff}

    Microseconds are included so rapid successive calls (e.g. watch mode)
    never collide on the same name.
    """
    project = _sanitize_project_name(Path(target_path).name)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return f"{project}_{mode}_{direction}_{ts}"


def _write_backup_meta(backup_dir, mode, direction, original_path, source, destination):
    """
    Write the .dirsync_meta.json sidecar at the root of a backup folder.

    Uses the same atomic temp-file + os.replace() pattern as save_config()
    to avoid ever leaving a half-written metadata file behind.
    """
    meta = {
        "mode":              mode,
        "direction":         direction,
        "original_path":     str(original_path),
        "created_at":        datetime.now().isoformat(),
        "config_source":     str(source),
        "config_destination": str(destination),
    }
    meta_path = backup_dir / ".dirsync_meta.json"
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=backup_dir, prefix=".dirsync_meta_tmp_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, meta_path)
    except (IOError, OSError) as e:
        cprint(f"[!] Could not write backup metadata: {e}", Colors.YELLOW)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def create_full_backup(path, direction, source, destination):
    """
    Copy the entire target directory tree under the centralized backup
    root, using the shared naming convention, then write the metadata
    sidecar. Replaces the old create_backup() for the "full" backup mode.
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        root = backup_root_dir()
    except OSError as e:
        cprint(f"[✗] Backup failed: could not create backup root ({e})", Colors.RED)
        return None
    backup = root / _backup_name(p, "full", direction)
    try:
        shutil.copytree(str(p), str(backup))
        _write_backup_meta(backup, "full", direction, p, source, destination)
        cprint(f"[✓] Backup created: {backup}", Colors.CYAN)
        return backup
    except Exception as e:
        cprint(f"[✗] Backup failed: {e}", Colors.RED)
        return None


def _focus_risk_files(to_copy, to_delete, direction, src_root, dst_root, delete_orphans):
    """
    Build the list of (kind, rel, current_path) files this sync pass is
    about to overwrite or delete — the exact set a "focus" backup must
    protect before apply_sync() runs.

    "new" to_copy entries are excluded: nothing exists yet at their
    target, so there is nothing to protect. The target path for a
    "modified" entry is resolved with the same _resolve_copy_target()
    apply_sync() itself uses (including the winner/loser resolution
    for "smart" direction), so the file backed up here is guaranteed
    to be exactly the one about to be overwritten — any divergence
    from apply_sync()'s own resolution would silently protect the
    wrong file.

    to_delete entries are only included when delete_orphans is active
    (ref_root mirrors apply_sync()'s own delete-phase root selection)
    — otherwise those files won't actually be removed this pass, so
    they aren't at risk.
    """
    risk = []
    for rel, v in to_copy.items():
        if "new" in v["tag"]:
            continue
        target = _resolve_copy_target(rel, v, direction, src_root, dst_root)
        risk.append(("copy", rel, target))
    if delete_orphans:
        ref_root = dst_root if direction in ("src", "smart") else src_root
        for rel in to_delete:
            risk.append(("delete", rel, ref_root / rel))
    return risk


def create_focus_backup(source, destination, to_copy, to_delete,
                         direction, delete_orphans):
    """
    Back up only the files actually at risk in this sync pass, each
    preserved at its exact original relative path under the backup
    root (a faithful subtree, not a flat dump) — see
    _focus_risk_files() for exactly which files qualify.

    Safety policy: unlike create_full_backup() (all-or-nothing), a
    failure backing up one specific file never aborts the whole
    backup. That file is instead pulled out of to_copy/to_delete
    before returning, so apply_sync() can never overwrite or delete
    the only existing copy of a file that failed to be saved. The
    caller (run_sync) derives the backup-failure error count itself
    by comparing the returned, filtered to_copy/to_delete against the
    originals it passed in — this keeps this function's return shape
    exactly the 3-tuple specified, while still surfacing every backup
    failure as a counted error in the final report.

    Each individual file copy uses _copy_atomic() unmodified — but
    sequentially, not through the ticket-3 copy pool: a backup step is
    a reliability operation, not a speed one, and staying sequential
    here keeps this first cut of "focus" simple to reason about.

    Root-level failures (backup root itself can't be created — e.g. a
    read-only CONFIG_DIR) are treated like create_full_backup(): a
    warning is printed and the sync proceeds with to_copy/to_delete
    unchanged, rather than retiring every at-risk file. Retiring
    everything would effectively block the whole sync on a single
    infrastructure failure, which contradicts the "never block a sync
    over a failed backup" behavior already established for "full".

    Returns (backup_path_or_None, filtered_to_copy, filtered_to_delete).
    """
    src_root = Path(source)
    dst_root = Path(destination)

    risk = _focus_risk_files(to_copy, to_delete, direction, src_root, dst_root, delete_orphans)
    if not risk:
        cprint("[i] No existing files to back up — backup skipped.", Colors.GREY)
        return None, to_copy, to_delete

    try:
        root = backup_root_dir()
    except OSError as e:
        cprint(f"[✗] Backup failed: could not create backup root ({e})", Colors.RED)
        return None, to_copy, to_delete

    # Naming/metadata mirror create_full_backup()'s own target selection:
    # the destination is the primary target for "src"/"smart", the
    # source for "dst" — same rule run_sync() already uses to pick
    # target_backup for the "full" mode call.
    target_root = destination if direction in ("src", "smart") else source
    backup = root / _backup_name(target_root, "focus", direction)

    failed_copy   = set()
    failed_delete = set()
    for kind, rel, current_path in risk:
        dst_file = backup / rel
        try:
            _copy_atomic(current_path, dst_file)
        except Exception as e:
            cprint(f"[✗] Focus backup failed for {rel}: {e}", Colors.RED)
            if kind == "copy":
                failed_copy.add(rel)
            else:
                failed_delete.add(rel)

    _write_backup_meta(backup, "focus", direction, target_root, source, destination)
    cprint(f"[✓] Backup created: {backup}", Colors.CYAN)

    filtered_to_copy   = {rel: v for rel, v in to_copy.items() if rel not in failed_copy}
    filtered_to_delete = [rel for rel in to_delete if rel not in failed_delete]

    return backup, filtered_to_copy, filtered_to_delete


# ─────────────────────────────────────────────────────────────────
#  SYNC ENGINE
# ─────────────────────────────────────────────────────────────────
def _copy_atomic(src, dst):
    """
    Copy src to dst atomically.
    Writes into a sibling temp file, then renames it into place via
    os.replace(). This means a partial file never appears at the
    final destination path, even if the process is interrupted mid-copy.

    Already thread-safe as-is: tempfile.mkstemp() hands out a unique
    path per call, so concurrent callers (the copy pool below) never
    collide on the same temp file, and os.replace() is atomic per
    destination path.
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


def _max_copy_workers():
    """
    Bound the copy thread pool.

    Copying is write I/O bound (disk writes at the destination), unlike
    hashing's read-bound workload, so it gets its own — slightly lower —
    ceiling: min(12, base * 2) instead of hashing's min(16, base * 2).
    Termux/Android keeps the same hard cap of 4 as hashing, for the same
    constrained-hardware / FUSE-overhead reasons documented on
    _max_hash_workers().
    """
    is_termux = bool(os.environ.get("TERMUX_VERSION"))
    base = os.cpu_count() or 2
    return 4 if is_termux else min(12, base * 2)


def _resolve_copy_target(rel, v, direction, src_root, dst_root):
    """
    Resolve the destination path for one to_copy entry.

    Identical logic to what apply_sync used to compute inline before
    parallelization — factored out only so the parallel copy task below
    can call it per-entry, inside its own thread, with no shared state.
    """
    if direction == "src":
        return dst_root / rel
    elif direction == "dst":
        return src_root / rel
    elif direction == "smart":
        winner = v.get("winner", "source")
        return dst_root / rel if winner == "source" else src_root / rel
    else:
        return dst_root / rel


def apply_sync(source, destination, to_copy, to_delete,
               direction="src", delete_orphans=False, dry_run=False):
    """
    Apply the selected file changes.
    Returns (success_count, error_count).

    Copy phase (to_copy): parallelized on a bounded thread pool (see
    _max_copy_workers()) when not in dry-run mode. Each task resolves
    its own destination and calls the existing _copy_atomic()
    unmodified, then returns a (ok, error_message) result instead of
    touching a shared counter — success/error counts are aggregated
    sequentially after every future has resolved, so there is no race
    on the counters and no lock is needed. Progress lines are still
    printed one file at a time (one cprint() call per line), but the
    order they appear in is no longer guaranteed to match the order of
    the to_copy dict, since copies now finish asynchronously. The
    ThreadPoolExecutor is used as a context manager so a Ctrl+C during
    copying still waits for in-flight tasks to finish/join cleanly
    before the interrupt propagates — no partial file can ever land at
    a final destination path, since that guarantee lives in
    _copy_atomic() itself and is untouched here.

    Delete phase (to_delete): stays strictly sequential, unchanged —
    orphan deletion is irreversible, so it is deliberately kept out of
    the parallel path regardless of backup_mode or file count.

    --dry-run: stays strictly sequential too (no writes happen, so
    there's no perf to gain), which also keeps its output order
    identical to the pre-parallelization implementation.
    """
    src_root = Path(source)
    dst_root = Path(destination)
    label    = "[DRY-RUN] " if dry_run else ""
    success  = errors = 0

    if dry_run:
        for rel, v in to_copy.items():
            tag   = v.get("tag", "modified")
            sym   = "+" if "new" in tag else "~"
            color = Colors.GREEN if sym == "+" else Colors.YELLOW
            cprint(f"  {label}[{sym}] {rel}", color)
            success += 1

    elif to_copy:
        def _copy_task(item):
            rel, v   = item
            frm      = Path(v["from"])
            tag      = v.get("tag", "modified")
            sym      = "+" if "new" in tag else "~"
            color    = Colors.GREEN if sym == "+" else Colors.YELLOW
            dst_file = _resolve_copy_target(rel, v, direction, src_root, dst_root)
            try:
                _copy_atomic(frm, dst_file)
                cprint(f"  [{sym}] {rel}", color)
                return True, None
            except Exception as e:
                cprint(f"  [{sym}] {rel}", color)
                cprint(f"       [✗] Error: {e}", Colors.RED)
                return False, str(e)

        with ThreadPoolExecutor(max_workers=_max_copy_workers()) as ex:
            for ok, _err in ex.map(_copy_task, to_copy.items()):
                if ok:
                    success += 1
                else:
                    errors += 1

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
#  BACKUP MANAGER — LISTING & RESTORE
# ─────────────────────────────────────────────────────────────────

# Sidecar filename is excluded from every backup-vs-target scan below —
# it is dirsync's own bookkeeping, written *after* the tree it describes,
# and must never be treated as a file to restore.
_BACKUP_META_NAME = ".dirsync_meta.json"


def _read_backup_meta(backup_dir):
    """
    Read and validate the .dirsync_meta.json sidecar for one backup folder.

    Returns the parsed dict, or None if the sidecar is missing, corrupt,
    or missing a required key. Callers must treat None as "unrecognized
    backup, restoration disabled" per the ticket — never as a reason to
    skip or delete the folder from the listing.
    """
    meta_path = backup_dir / _BACKUP_META_NAME
    if not meta_path.exists():
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except (json.JSONDecodeError, IOError):
        return None
    if not all(k in meta for k in ("mode", "direction", "original_path", "created_at")):
        return None
    if meta["mode"] not in ("full", "focus"):
        return None
    return meta


def _count_backup_files(backup_dir):
    """Count regular files under backup_dir, excluding the meta sidecar."""
    count = 0
    for _, _, files in os.walk(str(backup_dir)):
        count += sum(1 for f in files if f != _BACKUP_META_NAME)
    return count


def _list_backups():
    """
    Scan backup_root_dir() and return one entry per subfolder:
        {"dir": Path, "meta": dict_or_None, "file_count": int}

    Sorted by created_at descending. Folders without valid metadata sort
    last (empty-string sort key) but are always included — they are
    never silently ignored or deleted, only flagged as
    restoration-disabled by the caller.
    """
    root = backup_root_dir()
    entries = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        meta = _read_backup_meta(child)
        entries.append({
            "dir":         child,
            "meta":        meta,
            "file_count":  _count_backup_files(child),
        })
    entries.sort(key=lambda e: e["meta"]["created_at"] if e["meta"] else "", reverse=True)
    return entries


def _print_backups_list(entries):
    print_section("Available backups")
    if not entries:
        cprint("[i] No backups found.", Colors.GREY)
        print()
        return
    for i, e in enumerate(entries, 1):
        meta = e["meta"]
        if meta is None:
            cprint(f"  [{i:2d}] {e['dir'].name}", Colors.GREY)
            cprint("       ⚠ Missing/corrupted metadata — restore disabled.", Colors.YELLOW)
        else:
            cprint(f"  [{i:2d}] {meta['mode']:5s} | {meta['direction']:6s} | {e['file_count']} file(s)",
                   Colors.CYAN, bold=True)
            cprint(f"       Original : {meta['original_path']}", Colors.GREY)
            cprint(f"       Created  : {meta['created_at']}", Colors.GREY)
        print()


def _prompt_restore_target(original_path):
    """
    Ask where to restore to: the backup's recorded original location, or
    a manually entered folder. The resolved absolute path of the
    original location is shown up front — this is what surfaces a
    config that changed since the backup was made, before any
    destructive confirmation happens.

    Returns the resolved target Path. Raises UserCancelled on decline,
    invalid input, or Ctrl+C.
    """
    resolved_original = os.path.realpath(os.path.expanduser(original_path))
    print(f"  [1] Original location : {resolved_original}")
    print("  [2] Other folder (enter manually)")
    print("  [0] Cancel")
    try:
        choice = input("Choice: ").strip()
    except KeyboardInterrupt:
        print()
        raise UserCancelled("Restore cancelled.")

    if choice == "1":
        return Path(resolved_original)
    elif choice == "2":
        try:
            raw = input("Target folder path: ").strip()
        except KeyboardInterrupt:
            print()
            raise UserCancelled("Restore cancelled.")
        if not raw:
            raise UserCancelled("Restore cancelled.")
        try:
            return Path(os.path.realpath(os.path.expanduser(raw)))
        except (OSError, ValueError) as e:
            cprint(f"[✗] Invalid path: {e}", Colors.RED)
            raise UserCancelled("Restore cancelled.")
    else:
        raise UserCancelled("Restore cancelled.")


def _ensure_restore_target(target):
    """
    Mirror run_sync()'s missing-directory handling for the restore
    target: if it doesn't exist yet, offer to create it after explicit
    confirmation. Raises UserCancelled if declined or creation fails.
    """
    if target.exists():
        return
    cprint(f"[?] This folder doesn't exist: {target}", Colors.YELLOW)
    try:
        rep = input("    Create it? (y/n): ").strip().lower()
    except KeyboardInterrupt:
        print()
        raise UserCancelled("Restore cancelled.")
    if rep not in ("y", "yes"):
        raise UserCancelled("Restore cancelled.")
    try:
        target.mkdir(parents=True, exist_ok=True)
        cprint("[✓] Folder created.", Colors.GREEN)
    except OSError as e:
        cprint(f"[✗] Could not create folder: {e}", Colors.RED)
        raise UserCancelled("Restore cancelled.")


def _confirm_restore():
    """
    Two-stage confirmation required before any restore write happens: a
    standard y/n (right after the change report), then a literal typed
    'RESTORE' — not just 'y' — given how destructive this operation can
    be, especially a "full" restore forcing delete_orphans=True.
    Raises UserCancelled on any decline, mismatch, or Ctrl+C.
    """
    try:
        rep = input("Apply these changes? (y/n): ").strip().lower()
    except KeyboardInterrupt:
        print()
        raise UserCancelled("Restore cancelled.")
    if rep not in ("y", "yes"):
        raise UserCancelled("Restore cancelled.")

    print()
    cprint("Type RESTORE (uppercase) to confirm permanently:", Colors.RED, bold=True)
    try:
        typed = input("➜ ").strip()
    except KeyboardInterrupt:
        print()
        raise UserCancelled("Restore cancelled.")
    if typed != "RESTORE":
        raise UserCancelled("Restore cancelled (invalid confirmation).")
    print()


def _restore_full(entry, target, ensure_lock):
    """
    Restore a "full" backup as a classic sync where the backup plays the
    role of source: collect_files(backup) vs collect_files(target),
    detect_changes(direction="src"), print_report(), then apply_sync()
    with delete_orphans forced True. Reusing this exact pipeline means
    the restore automatically gets ticket 3's parallel copy pool and,
    critically, the same _mass_deletion_guard_triggered() check a normal
    sync relies on — a backup that looks suspiciously empty refuses to
    wipe out a target that still has files, exactly like a normal sync
    would refuse on an unavailable source.

    Note (flagged, not an architecture call): only the meta sidecar is
    excluded from the scan on both sides. No other ignore_patterns are
    applied — a "full" restore reproduces exactly what was physically
    backed up (create_full_backup() used a raw shutil.copytree(), not
    filtered scanning), regardless of the *current* ignore_patterns
    config, which may have changed since the backup was made.
    """
    backup_dir = entry["dir"]
    ignore = [_BACKUP_META_NAME]

    cprint("Scanning backup...", Colors.CYAN)
    backup_files = collect_files(str(backup_dir), ignore)
    target_files = collect_files(str(target), ignore)
    cprint(f"  Backup : {len(backup_files)} file(s)", Colors.GREY)
    cprint(f"  Target : {len(target_files)} file(s)", Colors.GREY)
    print()

    to_copy, to_delete = detect_changes(backup_files, target_files, direction="src")

    if _mass_deletion_guard_triggered(backup_files, target_files, True, "src"):
        cprint("[!] The backup appears empty while the target has files.", Colors.YELLOW)
        cprint("[!] Refusing to restore to avoid a mass deletion at the target.", Colors.RED)
        raise UserCancelled("Restore cancelled (anti mass-deletion guard).")

    has_changes = print_report(to_copy, to_delete, "src", True, backup_files, target_files)
    if not has_changes:
        cprint("[i] Nothing to restore — the target already matches the backup.", Colors.GREY)
        return

    _confirm_restore()

    if not ensure_lock():
        cprint("[✗] Another dirsync instance is already running.", Colors.RED)
        cprint(f"    Remove the lock file if this is wrong: {LOCK_FILE}", Colors.GREY)
        raise UserCancelled("Restore cancelled (lock active).")

    print_section("Restoring (full)...")
    success, errors = apply_sync(
        str(backup_dir), str(target), to_copy, to_delete,
        direction="src", delete_orphans=True, dry_run=False,
    )
    print()
    if errors == 0:
        cprint(f"[✓] Restore complete — {success} operation(s).", Colors.GREEN, bold=True)
    else:
        cprint(f"[~] Restore complete — {success} succeeded, {errors} error(s).", Colors.YELLOW, bold=True)


def _restore_focus(entry, target, ensure_lock):
    """
    Restore a "focus" backup: overwrite-only, never deletes.

    Deliberately NOT routed through apply_sync(delete_orphans=True) —
    each file physically present in the backup is copied via
    _copy_atomic() straight to its matching relative path under target,
    in a dedicated loop. This keeps the "focus never deletes" guarantee
    structurally impossible to confuse with the "full" path above,
    rather than relying on a caller always remembering to pass
    delete_orphans=False.
    """
    backup_dir = entry["dir"]
    ignore = [_BACKUP_META_NAME]

    cprint("Scanning backup...", Colors.CYAN)
    backup_files = collect_files(str(backup_dir), ignore)
    target_files = collect_files(str(target), ignore)
    cprint(f"  Backup : {len(backup_files)} file(s)", Colors.GREY)
    print()

    # Shaped like detect_changes()'s direction="src" output purely to
    # reuse print_report() for the confirmation summary. No to_delete is
    # ever built: a focus restore never removes anything at the target,
    # regardless of what's missing there relative to the backup.
    to_copy = {}
    for rel, info in backup_files.items():
        if rel in target_files:
            to_copy[rel] = {
                "from": info["full"], "tag": "modified",
                "src_info": info, "dst_info": target_files[rel],
            }
        else:
            to_copy[rel] = {"from": info["full"], "tag": "new", "info": info}

    has_changes = print_report(to_copy, [], "src", False, backup_files, target_files)
    if not has_changes:
        cprint("[i] Nothing to restore — the target already matches the backup.", Colors.GREY)
        return

    _confirm_restore()

    if not ensure_lock():
        cprint("[✗] Another dirsync instance is already running.", Colors.RED)
        cprint(f"    Remove the lock file if this is wrong: {LOCK_FILE}", Colors.GREY)
        raise UserCancelled("Restore cancelled (lock active).")

    print_section("Restoring (focus — overwrite only)...")
    success = errors = 0
    for rel, info in backup_files.items():
        dst_file = target / rel
        try:
            _copy_atomic(Path(info["full"]), dst_file)
            cprint(f"  [~] {rel}", Colors.YELLOW)
            success += 1
        except Exception as e:
            cprint(f"  [~] {rel}", Colors.YELLOW)
            cprint(f"       [✗] Error: {e}", Colors.RED)
            errors += 1
    print()
    if errors == 0:
        cprint(f"[✓] Restore complete — {success} file(s) restored.", Colors.GREEN, bold=True)
    else:
        cprint(f"[~] Restore complete — {success} succeeded, {errors} error(s).", Colors.YELLOW, bold=True)


def _restore_backup(entry, ensure_lock):
    """Resolve the target and dispatch to the mode-specific restore path."""
    meta = entry["meta"]
    print_section(f"Restore — {meta['mode']} / {meta['direction']}")
    target = _prompt_restore_target(meta["original_path"])
    _ensure_restore_target(target)
    print()
    if meta["mode"] == "full":
        _restore_full(entry, target, ensure_lock)
    else:
        _restore_focus(entry, target, ensure_lock)


def run_backup_manager():
    """
    Interactive backup listing/restoration manager, entered via
    --backups — independent of the normal sync flow (main() branches
    here before any direction prompt, path validation, or run_sync
    call).

    The PID lock is acquired lazily, only right before the first actual
    restore write in this session (not just for browsing/listing), and
    then held for the rest of the process — mirroring how main() itself
    acquires the lock once per process lifetime rather than per
    operation. acquire_lock() would otherwise refuse its own second call
    within the same still-running process (the lock file would contain
    our own, still-alive PID), so a local flag prevents re-acquiring
    what this session already holds.
    """
    lock_held = [False]

    def ensure_lock():
        if lock_held[0]:
            return True
        if acquire_lock():
            lock_held[0] = True
            return True
        return False

    while True:
        entries = _list_backups()
        _print_backups_list(entries)
        if not entries:
            return

        try:
            raw = input("Backup number to restore (0 to cancel): ").strip()
        except KeyboardInterrupt:
            print()
            return

        if raw in ("", "0"):
            return

        try:
            idx = int(raw)
        except ValueError:
            cprint("[?] Invalid input.", Colors.GREY)
            print()
            continue
        if not (1 <= idx <= len(entries)):
            cprint("[?] Number out of range.", Colors.GREY)
            print()
            continue

        entry = entries[idx - 1]
        if entry["meta"] is None:
            cprint("[✗] This backup has no valid metadata — restore disabled.", Colors.RED)
            print()
            continue

        try:
            _restore_backup(entry, ensure_lock)
        except UserCancelled as e:
            cprint(f"[i] {e}", Colors.GREY)
        print()


# ─────────────────────────────────────────────────────────────────
#  SINGLE SYNC PASS
# ─────────────────────────────────────────────────────────────────
def _mass_deletion_guard_triggered(src_files, dst_files, delete_orphans, direction):
    """
    Shared anti-mass-deletion guard.

    Refuses to proceed when the source-side scan came back empty while
    the destination-side scan still has files and delete_orphans is
    active for a direction that would delete them — the source having
    zero files is far more likely to mean "unavailable" (unmounted
    drive, network share, or — for a restore — a mistakenly empty
    backup folder) than "genuinely emptied on purpose".

    Factored out of run_sync() (unchanged behavior/messages there) so
    ticket 7's "full" backup restore, which is deliberately implemented
    as a classic sync with the backup playing the role of source, can
    call the exact same check instead of a re-implementation that could
    silently drift from it over time.
    """
    return (not src_files and dst_files
            and delete_orphans
            and direction in ("src", "smart"))


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
    if _mass_deletion_guard_triggered(src_files, dst_files, config["delete_orphans"], direction):
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
    backup_errors = 0
    if config["backup_before_sync"]:
        print_section("Creating backup...")
        target_backup = destination if direction in ("src", "smart") else source
        if config["backup_mode"] == "full":
            create_full_backup(target_backup, direction, source, destination)
        elif config["backup_mode"] == "focus":
            # create_focus_backup() returns to_copy/to_delete with any
            # entry it couldn't back up already removed — comparing
            # counts before/after is how the caller learns how many
            # backup failures happened, since the function's own return
            # shape stays the 3-tuple the ticket specifies.
            before_copy, before_delete = len(to_copy), len(to_delete)
            _, to_copy, to_delete = create_focus_backup(
                source, destination, to_copy, to_delete,
                direction, config["delete_orphans"],
            )
            backup_errors = (before_copy - len(to_copy)) + (before_delete - len(to_delete))
        print()

    # Apply changes
    print_section("Syncing files...")
    success, errors = apply_sync(
        source, destination, to_copy, to_delete,
        direction, config["delete_orphans"], dry_run=False,
    )
    # Files a focus backup couldn't protect were already removed from
    # to_copy/to_delete above (never synced/deleted this pass), but the
    # failure must still be counted and visible in the final report.
    errors += backup_errors

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
def _bare_invocation(args):
    """
    True when the CLI invocation carries none of the mode-selecting
    flags — the only case where run_main_menu() (ticket 8) is shown.
    Any of these flags preserves the exact pre-ticket-8 behavior: no
    new menu, direct flow into the existing direction-prompt/run_sync
    sequence in main().

    --config is included per the ticket's own condition, but main()
    already returns above before this function is ever called whenever
    it's set (same for --backups, checked just above it) — listed here
    only to document precedence, never the deciding factor at runtime.
    """
    return not (
        args.auto or args.watch or args.config
        or args.direction or args.pick or args.dry_run or args.ext
    )


def run_main_menu(config):
    """
    Interactive entry-point menu (ticket 8), shown only for a bare
    invocation — see _bare_invocation(). Replaces the previous direct
    fall-through from show_config() into the direction prompt; that
    prompt (and run_sync) is untouched, just reached one step later.

    Loops after [2] Settings or [3] Backup manager so the
    user always lands back here — neither call can exit the process on
    its own. Only [1] returns normally (falling through in main() to
    the existing direction-prompt/run_sync flow, unchanged); [0] and
    Ctrl+C both exit the whole process directly here, matching the
    pattern already used for the direction prompt below.
    """
    while True:
        print_section("Main menu")
        print("  [1] Start a sync")
        print("  [2] Settings")
        print("  [3] Backup manager")
        print("  [0] Quit")
        print()
        try:
            choice = input("Choice: ").strip()
        except KeyboardInterrupt:
            print()
            cprint("[✗] Interrupted. Goodbye.", Colors.YELLOW)
            sys.exit(0)

        if choice == "1":
            return
        elif choice == "2":
            run_config_menu(config)
            print()
        elif choice == "3":
            run_backup_manager()
            print()
        elif choice == "0":
            cprint("Goodbye.", Colors.GREY)
            sys.exit(0)
        else:
            cprint("[?] Invalid choice.", Colors.GREY)
            print()


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
    parser.add_argument("--backups",   action="store_true",
                        help="Open the backup browser/restore manager")
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

    # Backup browser/restore manager — independent of the sync flow
    # entirely: no source/destination validation, no first-run wizard,
    # no direction prompt. Precedence matches --config above.
    if args.backups:
        run_backup_manager()
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

    # New interactive main menu (ticket 8) — bare invocation only. Any
    # of the mode-selecting flags checked by _bare_invocation() skips
    # this entirely and falls straight through to the lock/direction
    # prompt/run_sync sequence below, exactly as before this ticket.
    # run_main_menu() only returns once the user picked [1] Start a
    # sync; [2]/[3] loop internally, [0]/Ctrl+C exit(0).
    if _bare_invocation(args):
        run_main_menu(config)

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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dirsync.py
----------
A zero-dependency, interactive file synchronizer for any two directories.

Designed to work on any platform: Linux, macOS, Windows, Android (Termux).
Great for syncing between editors, storage locations, or any pair of folders.

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
    python dirsync.py --config                 # open settings menu
    python dirsync.py --version
    python dirsync.py --help
"""

__version__ = "1.0.0"
__author__  = "FuegoDev"
__license__ = "MIT"

import os
import sys
import json
import shutil
import hashlib
import argparse
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
_SHARED_ROOTS  = ("/storage/", "/sdcard/", "/mnt/sdcard/")
_in_shared     = any(str(SCRIPT_DIR).startswith(r) for r in _SHARED_ROOTS)
CONFIG_DIR     = Path.home() if _in_shared else SCRIPT_DIR
CONFIG_FILE    = CONFIG_DIR / ".dirsync_config.json"
LOG_FILE       = CONFIG_DIR / ".dirsync.log"

DEFAULT_CONFIG = {
    "source":           "",
    "destination":      "",
    "ignore_patterns":  [
        "node_modules",
        ".git",
        ".vite",
        "dist",
        "build",
        ".cache",
        "__pycache__",
        "*.log",
        "package-lock.json",
        ".DS_Store",
        "Thumbs.db"
    ],
    "delete_orphans":    False,
    "backup_before_sync": False
}


# ─────────────────────────────────────────────────────────────────
#  TERMINAL COLORS
# ─────────────────────────────────────────────────────────────────
class C:
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

def cp(msg, color=C.RESET, bold=False):
    prefix = C.BOLD if bold else ""
    print(f"{prefix}{color}{msg}{C.RESET}")

def sep(label="", color=C.CYAN):
    if label:
        cp(f"── {label} ──", color, bold=True)
    else:
        cp("─" * 52, C.GREY)

def banner():
    print()
    cp("╔══════════════════════════════════════════════════╗", C.CYAN, bold=True)
    cp(f"║           dirsync  v{__version__}  by FuegoDev           ║", C.CYAN, bold=True)
    cp("║       Two-directory file synchronizer           ║", C.CYAN, bold=True)
    cp("╚══════════════════════════════════════════════════╝", C.CYAN, bold=True)
    print()


# ─────────────────────────────────────────────────────────────────
#  CONFIG — LOAD / SAVE / DISPLAY
# ─────────────────────────────────────────────────────────────────
def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
            for key, val in DEFAULT_CONFIG.items():
                config.setdefault(key, val)
            return config
        except (json.JSONDecodeError, IOError) as e:
            cp(f"[!] Corrupted config ({e}), using defaults.", C.YELLOW)
    return dict(DEFAULT_CONFIG)


def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        cp(f"[✓] Config saved → {CONFIG_FILE}", C.GREEN)
    except IOError as e:
        cp(f"[✗] Could not save config: {e}", C.RED)
        cp("[!] Tip: make sure the script directory is writable.", C.YELLOW)


def show_config(config):
    sep("Current configuration")
    src = config["source"] or "(not set)"
    dst = config["destination"] or "(not set)"
    cp(f"  Source          : {src}", C.BLUE)
    cp(f"  Destination     : {dst}", C.BLUE)
    cp(f"  Ignored         : {', '.join(config['ignore_patterns'])}", C.GREY)
    cp(f"  Delete orphans  : {'Yes' if config['delete_orphans'] else 'No'}", C.GREY)
    cp(f"  Auto backup     : {'Yes' if config['backup_before_sync'] else 'No'}", C.GREY)
    print()


def run_config_menu(config):
    """Interactive settings menu."""
    show_config(config)
    cp("What would you like to change?", C.YELLOW, bold=True)
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
        cp("\n[i] Config menu closed.", C.GREY)
        return config

    try:
        if choice == "1":
            v = input(f"New source path [{config['source']}]: ").strip()
            if v:
                config["source"] = os.path.expanduser(v)
        elif choice == "2":
            v = input(f"New destination path [{config['destination']}]: ").strip()
            if v:
                config["destination"] = os.path.expanduser(v)
        elif choice == "3":
            cur = "Yes" if config["delete_orphans"] else "No"
            v = input(f"Delete orphan files? (y/n) [{cur}]: ").strip().lower()
            if v in ("y", "yes"):
                config["delete_orphans"] = True
            elif v in ("n", "no"):
                config["delete_orphans"] = False
        elif choice == "4":
            cur = "Yes" if config["backup_before_sync"] else "No"
            v = input(f"Enable auto-backup? (y/n) [{cur}]: ").strip().lower()
            if v in ("y", "yes"):
                config["backup_before_sync"] = True
            elif v in ("n", "no"):
                config["backup_before_sync"] = False
        elif choice == "5":
            print("\nCurrent patterns:", ", ".join(config["ignore_patterns"]))
            print("  [a] Add a pattern")
            print("  [r] Remove a pattern")
            sub = input("Choice: ").strip().lower()
            if sub == "a":
                p = input("Pattern to add (e.g. *.tmp): ").strip()
                if p and p not in config["ignore_patterns"]:
                    config["ignore_patterns"].append(p)
                    cp(f"[+] '{p}' added.", C.GREEN)
            elif sub == "r":
                p = input("Pattern to remove: ").strip()
                if p in config["ignore_patterns"]:
                    config["ignore_patterns"].remove(p)
                    cp(f"[-] '{p}' removed.", C.YELLOW)
                else:
                    cp("[?] Pattern not found.", C.GREY)
    except KeyboardInterrupt:
        print()
        cp("\n[i] Config menu closed.", C.GREY)
        return config

    save_config(config)
    return config


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
            cp("\n[✗] Setup interrupted. No config saved.", C.YELLOW)
            sys.exit(0)
        if value:
            return os.path.expanduser(value)
        cp("  [!] This field is required — please enter a path.", C.YELLOW)


def first_run_wizard(config):
    """
    Guide the user through setting source and destination paths.
    Both fields are mandatory — the wizard re-prompts until filled.
    Only saves config once both paths are provided.
    """
    missing_src = not config["source"]
    missing_dst = not config["destination"]

    cp("Let's configure your sync paths.", C.CYAN, bold=True)
    cp(f"(Config will be saved to: {CONFIG_FILE})", C.GREY)
    print()

    if missing_src:
        config["source"] = _prompt_path("Source directory path     ")
    if missing_dst:
        config["destination"] = _prompt_path("Destination directory path")

    save_config(config)
    print()
    return config


# ─────────────────────────────────────────────────────────────────
#  FILE UTILITIES
# ─────────────────────────────────────────────────────────────────
def file_hash(filepath):
    """Return the MD5 hash of a file's content, or None on error."""
    h = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except (IOError, PermissionError):
        return None


def fmt_mtime(filepath):
    try:
        ts = os.path.getmtime(filepath)
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d  %H:%M:%S")
    except OSError:
        return "unknown date"


def raw_mtime(filepath):
    try:
        return os.path.getmtime(filepath)
    except OSError:
        return 0


def should_ignore(path_str, ignore_patterns):
    """Return True if any part of the path matches an ignore pattern."""
    parts = Path(path_str).parts
    for part in parts:
        for pattern in ignore_patterns:
            if pattern.startswith("*"):
                if part.endswith(pattern[1:]):
                    return True
            elif part == pattern:
                return True
    return False


def collect_files(root_dir, ignore_patterns, ext_filter=None):
    """
    Walk root_dir recursively and return a dict:
        { relative_path: { hash, mtime, mtime_raw, full } }

    ext_filter: list of extensions to keep (e.g. ['.jsx', '.css']),
                or None to include everything.
    """
    root  = Path(root_dir)
    files = {}
    if not root.exists():
        return files

    for fpath in root.rglob("*"):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(root))
        if should_ignore(rel, ignore_patterns):
            continue
        if ext_filter and fpath.suffix.lower() not in ext_filter:
            continue
        files[rel] = {
            "hash":      file_hash(fpath),
            "mtime":     fmt_mtime(fpath),
            "mtime_raw": raw_mtime(fpath),
            "full":      str(fpath)
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
        'smart' → newest file on either side wins

    Returns:
        to_copy   : dict of files to copy
        to_delete : list of relative paths to remove
        conflicts : files where smart mode resolved a conflict
    """
    src_set  = set(src_files.keys())
    dst_set  = set(dst_files.keys())
    common   = src_set & dst_set
    to_copy  = {}
    to_delete = []
    conflicts = []

    if direction == "src":
        for rel in sorted(src_set - dst_set):
            to_copy[rel] = {
                "from": src_files[rel]["full"],
                "tag":  "new",
                "info": src_files[rel]
            }
        for rel in sorted(common):
            if src_files[rel]["hash"] != dst_files[rel]["hash"]:
                to_copy[rel] = {
                    "from":     src_files[rel]["full"],
                    "tag":      "modified",
                    "src_info": src_files[rel],
                    "dst_info": dst_files[rel]
                }
        to_delete = sorted(dst_set - src_set)

    elif direction == "dst":
        for rel in sorted(dst_set - src_set):
            to_copy[rel] = {
                "from": dst_files[rel]["full"],
                "tag":  "new",
                "info": dst_files[rel]
            }
        for rel in sorted(common):
            if src_files[rel]["hash"] != dst_files[rel]["hash"]:
                to_copy[rel] = {
                    "from":     dst_files[rel]["full"],
                    "tag":      "modified",
                    "src_info": dst_files[rel],
                    "dst_info": src_files[rel]
                }
        to_delete = sorted(src_set - dst_set)

    elif direction == "smart":
        for rel in sorted(src_set - dst_set):
            to_copy[rel] = {
                "from":   src_files[rel]["full"],
                "tag":    "new (src)",
                "info":   src_files[rel],
                "winner": "source"
            }
        for rel in sorted(dst_set - src_set):
            to_copy[rel] = {
                "from":   dst_files[rel]["full"],
                "tag":    "new (dst)",
                "info":   dst_files[rel],
                "winner": "destination"
            }
        for rel in sorted(common):
            si = src_files[rel]
            di = dst_files[rel]
            if si["hash"] == di["hash"]:
                continue
            if si["mtime_raw"] >= di["mtime_raw"]:
                winner, frm, wi, li = "source",      si["full"], si, di
            else:
                winner, frm, wi, li = "destination", di["full"], di, si
            to_copy[rel] = {
                "from":       frm,
                "tag":        "modified",
                "winner":     winner,
                "win_info":   wi,
                "lose_info":  li
            }
            conflicts.append(rel)

    return to_copy, to_delete, conflicts


# ─────────────────────────────────────────────────────────────────
#  CHANGE REPORT
# ─────────────────────────────────────────────────────────────────
def print_report(to_copy, to_delete, conflicts, direction,
                 delete_orphans, src_files, dst_files):
    """Print a detailed change report. Returns True if changes exist."""
    total = len(to_copy) + (len(to_delete) if delete_orphans else 0)

    dir_labels = {
        "src":   "Source  ➜  Destination",
        "dst":   "Destination  ➜  Source",
        "smart": "Bidirectional smart (newest wins)"
    }
    cp(f"  Mode: {dir_labels.get(direction, direction)}", C.MAGENTA)
    print()

    if not to_copy and not to_delete:
        cp("✓ No changes — both directories are identical.", C.GREEN, bold=True)
        return False

    new_files = {r: v for r, v in to_copy.items() if "new" in v["tag"]}
    modified  = {r: v for r, v in to_copy.items() if v["tag"] == "modified"}

    sep(f"{total} change(s) detected", C.YELLOW)
    print()

    if new_files:
        cp(f"  [+] {len(new_files)} new file(s):", C.GREEN, bold=True)
        for rel, v in new_files.items():
            info = v.get("info", {})
            label = f"  [{v['winner']}]" if "winner" in v else ""
            cp(f"       + {rel}{label}", C.GREEN)
            cp(f"         Added on : {info.get('mtime', '?')}", C.GREY)
        print()

    if modified:
        cp(f"  [~] {len(modified)} modified file(s):", C.YELLOW, bold=True)
        for rel, v in modified.items():
            if direction == "smart":
                wi = v.get("win_info", {})
                li = v.get("lose_info", {})
                cp(f"       ~ {rel}", C.YELLOW)
                cp(f"         ✓ {v['winner']:11s} (kept)    : {wi.get('mtime', '?')}", C.GREEN)
                cp(f"         ✗ {'source' if v['winner'] == 'destination' else 'destination':11s} (skipped) : {li.get('mtime', '?')}", C.GREY)
            else:
                si = v.get("src_info", {})
                di = v.get("dst_info", {})
                cp(f"       ~ {rel}", C.YELLOW)
                cp(f"         Source      modified : {si.get('mtime', '?')}", C.GREY)
                cp(f"         Destination modified : {di.get('mtime', '?')}", C.GREY)
        print()

    if to_delete:
        ref = dst_files if direction in ("src", "smart") else src_files
        if delete_orphans:
            cp(f"  [-] {len(to_delete)} orphan file(s) to delete:", C.RED, bold=True)
        else:
            cp(f"  [?] {len(to_delete)} orphan file(s) (ignored — deletion disabled):", C.GREY, bold=True)
        for rel in to_delete:
            info  = ref.get(rel, {})
            color = C.RED if delete_orphans else C.GREY
            mark  = "-" if delete_orphans else "?"
            cp(f"       {mark} {rel}", color)
            cp(f"         Last modified : {info.get('mtime', '?')}", C.GREY)
        print()

    return True


# ─────────────────────────────────────────────────────────────────
#  MANUAL FILE SELECTION  (--pick)
# ─────────────────────────────────────────────────────────────────
def pick_files(to_copy, to_delete):
    """
    Let the user choose exactly which files to include in the sync.
    Returns filtered (to_copy, to_delete).
    """
    items = []
    for rel, v in to_copy.items():
        items.append(("copy", rel, v))
    for rel in to_delete:
        items.append(("delete", rel, None))

    if not items:
        return to_copy, to_delete

    sep("Manual file selection", C.CYAN)
    cp("  Enter numbers to include (e.g. 1 3 5), 'all' or '0' to cancel.", C.WHITE)
    print()

    for i, (action, rel, v) in enumerate(items, 1):
        if action == "copy":
            tag   = v.get("tag", "?")
            sym   = "+" if "new" in tag else "~"
            color = C.GREEN if "new" in tag else C.YELLOW
            cp(f"  [{i:2d}] {sym} {rel}", color)
        else:
            cp(f"  [{i:2d}] - {rel}  (orphan)", C.RED)

    print()
    try:
        raw = input("Selected numbers (or 'all'): ").strip().lower()
    except KeyboardInterrupt:
        print()
        cp("\n[✗] Selection cancelled.", C.YELLOW)
        sys.exit(0)

    if raw == "0":
        cp("Cancelled.", C.GREY)
        sys.exit(0)

    if raw in ("all", "a"):
        return to_copy, to_delete

    try:
        indices = {int(x) for x in raw.split()}
    except ValueError:
        cp("[!] Invalid input — all files will be processed.", C.YELLOW)
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

    cp(f"\n[✓] {len(selected_copy) + len(selected_delete)} file(s) selected.", C.CYAN)
    print()
    return selected_copy, selected_delete


# ─────────────────────────────────────────────────────────────────
#  BACKUP
# ─────────────────────────────────────────────────────────────────
def create_backup(path):
    """Copy the target directory to a timestamped backup folder."""
    p  = Path(path)
    if not p.exists():
        return None
    ts     = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = p.parent / f"{p.name}_backup_{ts}"
    try:
        shutil.copytree(str(p), str(backup))
        cp(f"[✓] Backup created: {backup}", C.CYAN)
        return backup
    except Exception as e:
        cp(f"[✗] Backup failed: {e}", C.RED)
        return None


# ─────────────────────────────────────────────────────────────────
#  SYNC ENGINE
# ─────────────────────────────────────────────────────────────────
def apply_sync(source, destination, to_copy, to_delete,
               direction="src", delete_orphans=False, dry_run=False):
    """
    Apply the selected file changes.
    In 'dst' and 'smart' modes the copy target is computed
    dynamically from each entry's 'winner' field.
    """
    src_root = Path(source)
    dst_root = Path(destination)
    label    = "[DRY-RUN] " if dry_run else ""
    success  = errors = 0

    for rel, v in to_copy.items():
        frm   = Path(v["from"])
        tag   = v.get("tag", "modified")
        sym   = "+" if "new" in tag else "~"
        color = C.GREEN if sym == "+" else C.YELLOW

        if direction == "src":
            dst_file = dst_root / rel
        elif direction == "dst":
            dst_file = src_root / rel
        elif direction == "smart":
            winner   = v.get("winner", "source")
            dst_file = dst_root / rel if winner == "source" else src_root / rel
        else:
            dst_file = dst_root / rel

        cp(f"  {label}[{sym}] {rel}", color)

        if not dry_run:
            try:
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(frm), str(dst_file))
                success += 1
            except Exception as e:
                cp(f"       [✗] Error: {e}", C.RED)
                errors += 1
        else:
            success += 1

    if delete_orphans:
        ref_root = dst_root if direction in ("src", "smart") else src_root
        for rel in to_delete:
            target = ref_root / rel
            cp(f"  {label}[-] {rel}", C.RED)
            if not dry_run:
                try:
                    target.unlink()
                    success += 1
                except Exception as e:
                    cp(f"       [✗] Delete error: {e}", C.RED)
                    errors += 1
            else:
                success += 1

    return success, errors


# ─────────────────────────────────────────────────────────────────
#  LOG WRITER
# ─────────────────────────────────────────────────────────────────
def write_log(source, destination, direction, success, errors, to_copy, to_delete):
    """Append a sync summary entry to the log file."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"\n[{ts}]",
        f"  source      : {source}",
        f"  destination : {destination}",
        f"  direction   : {direction}",
        f"  copied      : {len(to_copy)}",
        f"  deleted     : {len(to_delete)}",
        f"  success     : {success}",
        f"  errors      : {errors}",
    ]
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        cp(f"[✓] Log updated → {LOG_FILE}", C.GREY)
    except IOError as e:
        cp(f"[!] Could not write log: {e}", C.YELLOW)


# ─────────────────────────────────────────────────────────────────
#  SINGLE SYNC PASS
# ─────────────────────────────────────────────────────────────────
def run_sync(config, args):
    """
    Execute one full sync pass.
    Returns (success, errors) counts.
    """
    source      = os.path.expanduser(config["source"])
    destination = os.path.expanduser(config["destination"])
    ignore      = config["ignore_patterns"]

    ext_filter = None
    if args.ext:
        ext_filter = [e if e.startswith(".") else f".{e}" for e in args.ext]
        cp(f"[i] Extension filter active: {', '.join(ext_filter)}", C.MAGENTA)
        print()

    # Resolve sync direction
    direction = args.direction or "src"

    # Verify source and destination exist
    for label_str, p in [("Source", source), ("Destination", destination)]:
        if not Path(p).exists():
            cp(f"[?] {label_str} not found: {p}", C.YELLOW)
            try:
                rep = input(f"    Create this directory? (y/n): ").strip().lower()
            except KeyboardInterrupt:
                print()
                cp("\n[✗] Cancelled.", C.YELLOW)
                sys.exit(0)
            if rep in ("y", "yes"):
                try:
                    Path(p).mkdir(parents=True, exist_ok=True)
                    cp("[✓] Directory created.", C.GREEN)
                except Exception as e:
                    cp(f"[✗] Could not create directory: {e}", C.RED)
                    sys.exit(1)
            else:
                cp("Cancelled.", C.GREY)
                sys.exit(0)

    # Collect files
    cp("Scanning files...", C.CYAN)
    src_files = collect_files(source,      ignore, ext_filter)
    dst_files = collect_files(destination, ignore, ext_filter)
    cp(f"  Source      : {len(src_files)} file(s)", C.GREY)
    cp(f"  Destination : {len(dst_files)} file(s)", C.GREY)
    print()

    # Detect changes
    to_copy, to_delete, conflicts = detect_changes(src_files, dst_files, direction)

    has_changes = print_report(
        to_copy, to_delete, conflicts, direction,
        config["delete_orphans"], src_files, dst_files
    )

    if not has_changes:
        return 0, 0

    # Manual file selection
    if args.pick:
        to_copy, to_delete = pick_files(to_copy, to_delete)
        if not to_copy and not to_delete:
            cp("No files selected. Cancelled.", C.GREY)
            return 0, 0

    # Dry-run
    if args.dry_run:
        sep("DRY-RUN mode (simulation — no files modified)", C.MAGENTA)
        apply_sync(source, destination, to_copy, to_delete,
                   direction, config["delete_orphans"], dry_run=True)
        print()
        cp("[i] No files were modified.", C.MAGENTA)
        return 0, 0

    # Confirmation
    if not args.auto:
        cp("Apply these changes? (y/n): ", C.YELLOW, bold=True)
        try:
            rep = input("➜ ").strip().lower()
        except KeyboardInterrupt:
            print()
            cp("\n[✗] Sync cancelled.", C.YELLOW)
            sys.exit(0)
        if rep not in ("y", "yes"):
            cp("Sync cancelled.", C.GREY)
            return 0, 0
        print()

    # Backup
    if config["backup_before_sync"]:
        sep("Creating backup...")
        target_backup = destination if direction in ("src", "smart") else source
        create_backup(target_backup)
        print()

    # Apply
    sep("Syncing files...")
    success, errors = apply_sync(
        source, destination, to_copy, to_delete,
        direction, config["delete_orphans"], dry_run=False
    )

    # Summary
    print()
    sep("Summary")
    ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    if errors == 0:
        cp(f"  ✓ {success} operation(s) completed — {ts}", C.GREEN, bold=True)
    else:
        cp(f"  ~ {success} succeeded, {errors} error(s) — {ts}", C.YELLOW, bold=True)
    print()

    return success, errors


# ─────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="dirsync",
        description="dirsync — zero-dependency two-directory file synchronizer"
    )
    parser.add_argument("--dry-run",   action="store_true",
                        help="Simulate sync — no files are modified")
    parser.add_argument("--auto",      action="store_true",
                        help="Skip confirmation prompt")
    parser.add_argument("--config",    action="store_true",
                        help="Open the settings menu")
    parser.add_argument("--direction", choices=["src", "dst", "smart"], default=None,
                        help="src=Source→Dest (default), dst=Dest→Source, smart=bidirectional")
    parser.add_argument("--pick",      action="store_true",
                        help="Manually select files to sync")
    parser.add_argument("--ext",       nargs="+", default=None,
                        help="Filter by file extension(s): --ext jsx css js")
    parser.add_argument("--watch",     type=int, default=None, metavar="SECONDS",
                        help="Watch mode: repeat sync every N seconds automatically")
    parser.add_argument("--log",       action="store_true",
                        help=f"Append sync history to {LOG_FILE}")
    parser.add_argument("--source",    type=str,
                        help="Override source path for this run")
    parser.add_argument("--dest",      type=str,
                        help="Override destination path for this run")
    parser.add_argument("--version",   action="version",
                        version=f"dirsync v{__version__}")
    args = parser.parse_args()

    banner()

    config = load_config()

    # Settings menu
    if args.config:
        run_config_menu(config)
        return

    # CLI overrides
    if args.source:
        config["source"] = os.path.expanduser(args.source)
    if args.dest:
        config["destination"] = os.path.expanduser(args.dest)

    # First-run wizard: prompt for paths if not configured
    if not config["source"] or not config["destination"]:
        config = first_run_wizard(config)

    show_config(config)

    # Interactive direction prompt (only in interactive mode, not --watch or --auto)
    if args.direction is None and not args.auto and not args.watch:
        sep("Sync direction")
        print("  [1] Source  ➜  Destination   (default)")
        print("  [2] Destination  ➜  Source   (reverse)")
        print("  [3] Bidirectional smart       (newest wins)")
        print()
        try:
            raw = input("Direction (1/2/3) [1]: ").strip()
        except KeyboardInterrupt:
            print()
            cp("\n[✗] Cancelled.", C.YELLOW)
            sys.exit(0)
        args.direction = {"2": "dst", "3": "smart"}.get(raw, "src")
        print()

    if args.direction is None:
        args.direction = "src"

    # ── Watch mode ───────────────────────────────────────────────
    if args.watch:
        interval = args.watch
        cp(f"[i] Watch mode: syncing every {interval}s — Ctrl+C to stop.", C.CYAN, bold=True)
        print()
        try:
            while True:
                cp(f"\n── Sync @ {datetime.now().strftime('%H:%M:%S')} ──", C.CYAN)
                success, errors = run_sync(config, args)
                if args.log:
                    write_log(
                        config["source"], config["destination"],
                        args.direction, success, errors, {}, []
                    )
                time.sleep(interval)
        except KeyboardInterrupt:
            print()
            cp("[i] Watch mode stopped.", C.GREY)
            return

    # ── Single pass ──────────────────────────────────────────────
    success, errors = run_sync(config, args)

    if args.log:
        write_log(
            config["source"], config["destination"],
            args.direction, success, errors, {}, []
        )


# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        cp("\n[✗] Interrupted. Goodbye.", C.YELLOW)
        sys.exit(0)

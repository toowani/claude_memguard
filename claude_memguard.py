#!/usr/bin/env python3
"""claude_memguard: stop processes spawned by Claude / Claude Code from exhausting RAM.

Why: on 2026-10-15 four subagent-spawned Whisper large-v3 Python processes (~10 GB each,
~37 GB total on a 24 GB Mac with 27 GB free disk) exhausted memory; the kernel watchdog
panicked ("no checkins from watchdogd in 91 seconds") and the Mac rebooted.

What it does (every INTERVAL seconds, stdlib only):
  * finds the Claude desktop app / Claude Code CLI processes and all their DESCENDANTS
    (python, ffmpeg, node scripts, ...); the Claude processes themselves are never touched;
  * rule 1: a descendant whose resident memory exceeds PER_PROC_MB is terminated;
  * rule 2: if all descendants together exceed TOTAL_MB, the largest one is terminated;
  * rule 3: if the kernel memory level (kern.memorystatus_level, % of memory available)
    falls below LOW_LEVEL and descendants hold more than LOW_LEVEL_MIN_MB, the largest
    descendant is terminated;
  terminate = SIGTERM, then SIGKILL after 3 s; each action is logged and shown as a
  macOS notification.

Usage:
  python3 claude_memguard.py                 run forever (the LaunchAgent does this)
  python3 claude_memguard.py --once --dry    one scan, report only
  python3 claude_memguard.py --once --per-proc-mb 300   (testing)
Log: ~/Library/Logs/claude-memguard.log
Deliberate big job (e.g. one Whisper large-v3 run, ~10 GB): write a per-process limit in MB to
~/.claude_memguard_allow (e.g. `echo 12000 > ~/.claude_memguard_allow`); it is honoured for 3 hours
after the file was last written, and the total limit rises with it. Delete the file afterwards.
"""
import argparse
import os
import signal
import subprocess
import sys
import time

LOG = os.path.expanduser('~/Library/Logs/claude-memguard.log')
CLAUDE_MARKERS = ('/Claude.app/', '/claude-code/')   # app bundle and Claude Code CLI paths


def log(msg):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + msg
    print(line, flush=True)
    try:
        with open(LOG, 'a') as f:
            f.write(line + '\n')
    except OSError:
        pass


def notify(msg):
    try:
        subprocess.run(['osascript', '-e', 'display notification "%s" with title "Claude memguard"'
                        % msg.replace('"', "'")], timeout=5, capture_output=True)
    except Exception:
        pass


def processes():
    out = subprocess.run(['ps', '-axo', 'pid=,ppid=,rss=,comm='], capture_output=True, text=True).stdout
    procs = {}
    for ln in out.splitlines():
        parts = ln.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, rss, comm = int(parts[0]), int(parts[1]), int(parts[2]), parts[3]
        procs[pid] = {'pid': pid, 'ppid': ppid, 'mb': rss / 1024.0, 'comm': comm}
    return procs


def is_claude(p):
    return any(m in p['comm'] for m in CLAUDE_MARKERS)


def descendants(procs):
    """Processes below a Claude process that are not Claude processes themselves."""
    roots = {pid for pid, p in procs.items() if is_claude(p)}
    kids = {}
    for pid, p in procs.items():
        kids.setdefault(p['ppid'], []).append(pid)
    found, stack = {}, list(roots)
    while stack:
        cur = stack.pop()
        for k in kids.get(cur, []):
            if k in found or k in roots:
                continue
            p = procs[k]
            if is_claude(p):
                stack.append(k)            # nested Claude helper: walk through it, never target it
                continue
            found[k] = p
            stack.append(k)
    return found


def memory_level():
    try:
        return int(subprocess.run(['sysctl', '-n', 'kern.memorystatus_level'],
                                  capture_output=True, text=True).stdout.strip())
    except ValueError:
        return 100


def terminate(p, reason, dry):
    name = os.path.basename(p['comm'])
    msg = 'kill pid %d %s (%.1f GB): %s' % (p['pid'], name, p['mb'] / 1024, reason)
    if dry:
        log('DRY ' + msg)
        return
    log(msg)
    notify('Stopped %s (%.1f GB): %s' % (name, p['mb'] / 1024, reason))
    try:
        os.kill(p['pid'], signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(30):
        time.sleep(0.1)
        try:
            os.kill(p['pid'], 0)
        except ProcessLookupError:
            return
    try:
        os.kill(p['pid'], signal.SIGKILL)
        log('SIGKILL pid %d' % p['pid'])
    except ProcessLookupError:
        pass


ALLOW = os.path.expanduser('~/.claude_memguard_allow')


def limits(a):
    """(per_proc_mb, total_mb), raised temporarily by a fresh ~/.claude_memguard_allow."""
    try:
        if time.time() - os.path.getmtime(ALLOW) < 3 * 3600:
            mb = int(open(ALLOW).read().strip() or 0)
            if mb > a.per_proc_mb:
                return mb, max(a.total_mb, mb + 4096)
    except (OSError, ValueError):
        pass
    return a.per_proc_mb, a.total_mb


def scan(a):
    per_proc_mb, total_mb = limits(a)
    procs = processes()
    desc = descendants(procs)
    total = sum(p['mb'] for p in desc.values())
    level = memory_level()
    acted = False
    for p in sorted(desc.values(), key=lambda q: -q['mb']):
        if p['mb'] > per_proc_mb:
            terminate(p, 'over per-process limit %d MB' % per_proc_mb, a.dry)
            acted = True
    if not acted and desc:
        big = max(desc.values(), key=lambda q: q['mb'])
        if total > total_mb:
            terminate(big, 'Claude children total %.1f GB > %d MB' % (total / 1024, total_mb), a.dry)
            acted = True
        elif level < a.low_level and total > a.low_level_min_mb:
            terminate(big, 'system memory level %d%% < %d%%' % (level, a.low_level), a.dry)
            acted = True
    return desc, total, level, acted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--per-proc-mb', type=int, default=6144)
    ap.add_argument('--total-mb', type=int, default=12288)
    ap.add_argument('--low-level', type=int, default=12, help='kern.memorystatus_level threshold (%%)')
    ap.add_argument('--low-level-min-mb', type=int, default=3072)
    ap.add_argument('--interval', type=float, default=3.0)
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    if a.once:
        desc, total, level, _ = scan(a)
        print('memory level %d%%; %d Claude child processes, %.2f GB total' % (level, len(desc), total / 1024))
        for p in sorted(desc.values(), key=lambda q: -q['mb'])[:10]:
            print('  %7d %8.1f MB  %s' % (p['pid'], p['mb'], p['comm'][-90:]))
        return
    log('memguard started: per-proc %d MB, total %d MB, low level %d%%' % (a.per_proc_mb, a.total_mb, a.low_level))
    while True:
        try:
            scan(a)
        except Exception as e:             # keep guarding whatever happens
            log('scan error: %r' % e)
        time.sleep(a.interval)


if __name__ == '__main__':
    sys.exit(main())

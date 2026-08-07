#!/usr/bin/env python3
"""Race-resistant management for repository-owned symlinks."""

import argparse
import os
import secrets
import stat
import sys


def _identity(item):
    return (item.st_dev, item.st_ino, stat.S_IFMT(item.st_mode))


def _open_parent(target):
    target = os.path.abspath(target)
    parent = os.path.realpath(os.path.dirname(target))
    before = os.stat(parent, follow_symlinks=False)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parent, flags)
    try:
        opened = os.fstat(directory_fd)
    except BaseException:
        os.close(directory_fd)
        raise
    if _identity(opened) != _identity(before):
        os.close(directory_fd)
        raise RuntimeError("link parent changed during validation: %s" % parent)
    return directory_fd, os.path.basename(target)


def _entry(directory_fd, name):
    try:
        item = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    link_target = os.readlink(name, dir_fd=directory_fd) if stat.S_ISLNK(item.st_mode) else None
    return (_identity(item), link_target)


def install(source, target):
    source = os.path.abspath(source)
    directory_fd, name = _open_parent(target)
    try:
        current = _entry(directory_fd, name)
        if current is not None:
            if current[1] == source:
                return
            kind = "foreign symlink" if current[1] is not None else "non-symlink"
            raise RuntimeError("Refusing to replace %s: %s" % (kind, target))
        try:
            os.symlink(source, name, dir_fd=directory_fd)
        except FileExistsError:
            current = _entry(directory_fd, name)
            if current is None or current[1] != source:
                raise RuntimeError("link target changed during install: %s" % target)
    finally:
        os.close(directory_fd)


def uninstall(expected, target):
    expected = os.path.abspath(expected)
    parent = os.path.realpath(os.path.dirname(os.path.abspath(target)))
    if not os.path.isdir(parent):
        return
    directory_fd, name = _open_parent(target)
    quarantine = None
    try:
        current = _entry(directory_fd, name)
        if current is None:
            return
        if current[1] != expected:
            print("Leaving foreign link/path untouched: %s" % target, file=sys.stderr)
            return

        quarantine = ".unified-kanban-unlink-%s" % secrets.token_hex(16)
        os.rename(name, quarantine, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        moved = _entry(directory_fd, quarantine)
        if moved != current:
            if _entry(directory_fd, name) is None:
                os.rename(quarantine, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                quarantine = None
            raise RuntimeError("link changed during uninstall; foreign entry preserved: %s" % target)

        # The unpredictable quarantine name narrows the final unlink race, and
        # an identity/target recheck ensures an entry swapped before this point
        # is never deleted.
        if _entry(directory_fd, quarantine) != current:
            raise RuntimeError("quarantined link changed before removal: %s" % target)
        os.unlink(quarantine, dir_fd=directory_fd)
        quarantine = None
    finally:
        try:
            if quarantine is not None and _entry(directory_fd, quarantine) is not None:
                print(
                    "Preserved a concurrently changed link entry at %s/%s"
                    % (parent, quarantine),
                    file=sys.stderr,
                )
        finally:
            os.close(directory_fd)


def main(argv=None):
    """Install or remove one repository-owned symlink from command-line arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            install(args.source, args.target)
        else:
            uninstall(args.source, args.target)
    except (OSError, RuntimeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

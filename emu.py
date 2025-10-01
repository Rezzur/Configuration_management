#!/usr/bin/env python3
"""
emulator.py — Мини-эмулятор UNIX-подобной оболочки с VFS, конфигом и логированием в XML.

Поддерживает:
- REPL (prompt содержит имя VFS и текущий путь)
- Простой парсер (split по пробелам)
- Заглушки и реальные команды: ls, cd, whoami, rev, head, chown, exit
- CLI параметры: --vfs, --log, --start, --config
- Конфиг JSON (если указан — имеет приоритет над CLI)
- Логирование событий вызова команд в XML (включает имя пользователя)
- Start script execution (комментарии начинаются с '#')
- VFS загружается из JSON (все операции в памяти)
"""
import argparse
import json
import base64
import os
import sys
import datetime
import getpass
import xml.etree.ElementTree as ET
from typing import Dict, Any, Optional, List

# ---------------------------
# Data model: VFS in memory
# ---------------------------
class VNode:
    def __init__(self, name: str, ntype: str, owner: str = "root", mode: str = "rw", content: Optional[bytes] = None):
        self.name = name
        self.type = ntype  # 'dir' or 'file'
        self.owner = owner
        self.mode = mode
        self.content = content or b""
        self.children: Dict[str, 'VNode'] = {}  # only for dir

    def to_repr(self):
        if self.type == 'dir':
            return f"<dir {self.name} owner={self.owner} entries={len(self.children)}>"
        else:
            size = len(self.content)
            return f"<file {self.name} owner={self.owner} size={size}B>"

def build_vfs_from_dict(d: Dict[str, Any]) -> VNode:
    """
    Ожидаемый формат (пример):
    {
      "name": "myvfs",
      "root": {
         "type": "dir",
         "children": {
             "file.txt": {"type":"file", "content":"SGVsbG8=", "owner":"alice"},
             "dir1": {"type":"dir", "children": { ... } }
         }
      }
    }
    """
    name = d.get("name", "VFS")
    root_def = d.get("root")
    if not root_def:
        raise ValueError("VFS JSON missing 'root' object")
    def make_node(name: str, node_def: Dict[str, Any]) -> VNode:
        ntype = node_def.get("type")
        if ntype not in ("dir","file"):
            raise ValueError(f"Invalid node type {ntype} for {name}")
        owner = node_def.get("owner", "root")
        mode = node_def.get("mode", "rw")
        if ntype == "dir":
            node = VNode(name, "dir", owner=owner, mode=mode)
            for child_name, child_def in node_def.get("children", {}).items():
                node.children[child_name] = make_node(child_name, child_def)
            return node
        else:
            # file: content base64
            b64 = node_def.get("content", "")
            try:
                content = base64.b64decode(b64) if b64 else b""
            except Exception as e:
                raise ValueError(f"Bad base64 content for file {name}: {e}")
            return VNode(name, "file", owner=owner, mode=mode, content=content)
    root_node = make_node("/", root_def)  # name "/" for root
    root_node.vfs_name = name
    return root_node

# ---------------------------
# Utilities: path resolution
# ---------------------------
def split_path(path: str) -> List[str]:
    if path.strip() == "":
        return []
    parts = [p for p in path.split("/") if p != ""]
    return parts

def resolve_path(root: VNode, cwd_parts: List[str], path: str) -> (Optional[VNode], Optional[str]):
    """
    Resolve path string to a VNode and its parent path string for operations.
    Returns (node, error_message). If node==None, error_message explains.
    Supports absolute (/...) and relative paths, .. and .
    """
    if path == "" or path == ".":
        # current
        cur = root
        for p in cwd_parts:
            if p not in cur.children:
                return None, f"current directory broken: {p} missing"
            cur = cur.children[p]
        return cur, None
    if path.startswith("/"):
        parts = split_path(path)
        cur = root
    else:
        parts = cwd_parts + split_path(path)
        cur = root
    for p in parts:
        if p == ".":
            continue
        if p == "..":
            # go up: if parts enumerated we can't easily go up without parent pointers.
            # implement by re-resolving from root skipping last element
            # But easier: rebuild list and traverse.
            # We'll handle '..' in a simple stack manner:
            # convert parts to stack:
            stack = []
            if path.startswith("/"):
                stack = []
            else:
                stack = []
            # build stack by processing parts from beginning
            stack2 = []
            if path.startswith("/"):
                source_parts = parts
            else:
                # for relative we need cwd parts first then remaining; easier to compute via this call only when '..' present
                # fallback simple approach: recompute properly:
                combined = cwd_parts + split_path(path)
                stack2 = []
                for q in combined:
                    if q == ".":
                        continue
                    if q == "..":
                        if stack2:
                            stack2.pop()
                    else:
                        stack2.append(q)
                parts = stack2
                cur = root
                for q in parts:
                    if q not in cur.children:
                        return None, f"path not found: /{'/'.join(parts)}"
                    cur = cur.children[q]
                return cur, None
        if p not in cur.children:
            return None, f"path not found: {path}"
        cur = cur.children[p]
    return cur, None

def list_dir(node: VNode) -> List[str]:
    if node.type != 'dir':
        return [node.to_repr()]
    names = sorted(node.children.keys())
    return names

# ---------------------------
# Logging (XML)
# ---------------------------
def ensure_log_root(filepath: str):
    if not os.path.exists(filepath):
        root = ET.Element("log")
        tree = ET.ElementTree(root)
        tree.write(filepath, encoding="utf-8", xml_declaration=True)

def append_log_event(filepath: str, user: str, command: str, args: List[str]):
    try:
        if not os.path.exists(filepath):
            ensure_log_root(filepath)
        tree = ET.parse(filepath)
        root = tree.getroot()
        ev = ET.SubElement(root, "event")
        ev.set("time", datetime.datetime.utcnow().isoformat() + "Z")
        ev.set("user", user)
        cmd = ET.SubElement(ev, "command")
        cmd.text = command
        args_el = ET.SubElement(ev, "args")
        args_el.text = " ".join(args)
        tree.write(filepath, encoding="utf-8", xml_declaration=True)
    except Exception as e:
        print(f"Error writing log {filepath}: {e}", file=sys.stderr)

# ---------------------------
# Emulator (commands)
# ---------------------------
class Emulator:
    def __init__(self, vfs_root: VNode, log_path: Optional[str] = None, start_script: Optional[str] = None):
        self.root = vfs_root
        self.vfs_name = getattr(vfs_root, "vfs_name", "VFS")
        self.cwd_parts: List[str] = []  # list of names from root (empty = root)
        self.log_path = log_path
        self.start_script = start_script
        self.user = getpass.getuser() or "unknown"

    def prompt(self):
        cur = "/" if not self.cwd_parts else "/" + "/".join(self.cwd_parts)
        return f"[{self.vfs_name}]{cur}$ "

    def echo_and_log(self, command: str, args: List[str]):
        if self.log_path:
            append_log_event(self.log_path, self.user, command, args)

    def run_cmd(self, command: str, args: List[str]) -> Optional[str]:
        # log event
        self.echo_and_log(command, args)
        try:
            if command == "exit":
                print("Bye.")
                sys.exit(0)
            elif command == "ls":
                return self.cmd_ls(args)
            elif command == "cd":
                return self.cmd_cd(args)
            elif command == "whoami":
                return self.user
            elif command == "rev":
                return self.cmd_rev(args)
            elif command == "head":
                return self.cmd_head(args)
            elif command == "chown":
                return self.cmd_chown(args)
            else:
                return f"Unknown command: {command}"
        except Exception as e:
            return f"Error executing {command}: {e}"

    # Implementation of individual commands
    def cmd_ls(self, args: List[str]) -> str:
        path = args[0] if args else ""
        node, err = resolve_path(self.root, self.cwd_parts, path) if (path or args) else resolve_path(self.root, self.cwd_parts, "")
        if node is None:
            return f"ls: {err}"
        if node.type == "file":
            return node.name
        names = list_dir(node)
        out_lines = []
        for n in names:
            child = node.children[n]
            out_lines.append(f"{n}\t{child.type}\towner:{child.owner}\tsize:{len(child.content) if child.type=='file' else '-'}")
        return "\n".join(out_lines)

    def cmd_cd(self, args: List[str]) -> str:
        if not args:
            # go to root
            self.cwd_parts = []
            return ""
        path = args[0]
        node, err = resolve_path(self.root, self.cwd_parts, path)
        if node is None:
            return f"cd: {err}"
        if node.type != "dir":
            return f"cd: not a directory: {path}"
        # compute new cwd_parts
        if path.startswith("/"):
            self.cwd_parts = split_path(path)
        else:
            combined = self.cwd_parts + split_path(path)
            # normalize with .. and .
            stack = []
            for p in combined:
                if p == ".":
                    continue
                if p == "..":
                    if stack:
                        stack.pop()
                else:
                    stack.append(p)
            self.cwd_parts = stack
        return ""

    def cmd_rev(self, args: List[str]) -> str:
        if not args:
            return ""
        target = args[0]
        # if target is a path to file in VFS, try to fetch
        node, err = resolve_path(self.root, self.cwd_parts, target)
        if node is None:
            # treat as literal string
            combined = " ".join(args)
            return combined[::-1]
        if node.type == "dir":
            return f"rev: {target} is a directory"
        try:
            text = node.content.decode(errors='replace')
            return text[::-1]
        except Exception:
            return f"rev: cannot decode file {target}"

    def cmd_head(self, args: List[str]) -> str:
        # head [-n N] filename
        if not args:
            return "head: missing file operand"
        n = 10
        idx = 0
        if args[0] == "-n":
            if len(args) < 3:
                return "head: usage: head [-n N] filename"
            try:
                n = int(args[1])
            except:
                return "head: invalid number"
            idx = 2
        filename = args[idx]
        node, err = resolve_path(self.root, self.cwd_parts, filename)
        if node is None:
            return f"head: {err}"
        if node.type != "file":
            return f"head: {filename}: not a file"
        text = node.content.decode(errors='replace').splitlines()
        return "\n".join(text[:n])

    def cmd_chown(self, args: List[str]) -> str:
        # chown owner filename
        if len(args) < 2:
            return "chown: usage: chown owner path"
        owner = args[0]
        path = args[1]
        node, err = resolve_path(self.root, self.cwd_parts, path)
        if node is None:
            return f"chown: {err}"
        node.owner = owner
        return ""

    # REPL loop
    def repl(self):
        # if start script present, execute first (but still show REPL afterwards)
        if self.start_script:
            self.run_startup_script(self.start_script)
        try:
            while True:
                try:
                    line = input(self.prompt())
                except EOFError:
                    print()
                    break
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                cmd = parts[0]
                args = parts[1:]
                out = self.run_cmd(cmd, args)
                if out is not None and out != "":
                    print(out)
        except KeyboardInterrupt:
            print("\nInterrupted. Exiting.")
            sys.exit(0)

    def run_startup_script(self, path: str):
        if not os.path.exists(path):
            print(f"Start script not found: {path}")
            return
        print(f"--- Executing start script: {path} ---")
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    stripped = line.strip()
                    if stripped == "" or stripped.startswith("#"):
                        # show comment or blank line as-is
                        print(f"{line}")
                        continue
                    # show input as if user typed
                    print(f">>> {line}")
                    parts = stripped.split()
                    cmd = parts[0]
                    args = parts[1:]
                    out = self.run_cmd(cmd, args)
                    if out is not None and out != "":
                        print(out)
        except Exception as e:
            print(f"Error executing start script {path}: {e}")

# ---------------------------
# CLI and config
# ---------------------------
def load_config_file(cfg_path: str) -> Dict[str,str]:
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            # expect keys: vfs, log, start
            return {
                "vfs": data.get("vfs", None) or data.get("path_to_vfs", None),
                "log": data.get("log", None),
                "start": data.get("start", None)
            }
    except Exception as e:
        print(f"Error reading config file {cfg_path}: {e}", file=sys.stderr)
        return {}

def main():
    p = argparse.ArgumentParser(description="Simple shell emulator with VFS")
    p.add_argument("--vfs", help="Path to VFS JSON file", default=None)
    p.add_argument("--log", help="Path to XML log file", default=None)
    p.add_argument("--start", help="Path to start script", default=None)
    p.add_argument("--config", help="Path to JSON config file (overrides CLI)", default=None)
    args = p.parse_args()

    cli_settings = {"vfs": args.vfs, "log": args.log, "start": args.start}
    cfg_settings = {}
    if args.config:
        cfg_settings = load_config_file(args.config)
        if cfg_settings == {}:
            print("Warning: config file could not be read or is empty. Continuing with CLI values.", file=sys.stderr)
    # merge: file settings override CLI settings
    final = cli_settings.copy()
    for k, v in cfg_settings.items():
        if v:
            final[k] = v

    vfs_path = final.get("vfs")
    log_path = final.get("log")
    start_script = final.get("start")

    # load VFS
    if vfs_path:
        if not os.path.exists(vfs_path):
            print(f"Error: VFS file not found: {vfs_path}", file=sys.stderr)
            # create minimal VFS root to continue
            root = VNode("/", "dir")
            root.vfs_name = "VFS"
        else:
            try:
                with open(vfs_path, "r", encoding="utf-8") as f:
                    j = json.load(f)
                    root = build_vfs_from_dict(j)
            except Exception as e:
                print(f"Error loading VFS from {vfs_path}: {e}", file=sys.stderr)
                root = VNode("/", "dir")
                root.vfs_name = "VFS"
    else:
        root = VNode("/", "dir")
        root.vfs_name = "VFS"

    if log_path:
        try:
            ensure_log_root(log_path)
        except Exception as e:
            print(f"Error preparing log file {log_path}: {e}", file=sys.stderr)

    emulator = Emulator(root, log_path, start_script)
    # Debug print of parameters on startup (as required in stage 2)
    print("=== Emulator starting (debug) ===")
    print(f"VFS file: {vfs_path}")
    print(f"Log file: {log_path}")
    print(f"Start script: {start_script}")
    print(f"User: {emulator.user}")
    print("=================================")
    emulator.repl()

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
emulator_full.py — минимальный полнофункциональный эмулятор shell (Stages 1-5)

Запуск:
  python3 emulator_full.py [--vfs VFS_JSON] [--log LOG_XML] [--start START_SCRIPT] [--config CONFIG_JSON]

Поведение:
 - Значения из CONFIG_JSON имеют приоритет над CLI-параметрами (требование).
 - При старте печатает debug-параметры.
 - Загружает VFS из JSON (в памяти). Файлы: base64 content.
 - Логирует вызовы команд в XML с указанием user.
 - Поддерживает стартовый скрипт: строки начинающиеся с '#' — комментарии; прочие показываются как ввод (>>> ...), выводится результат.
 - Команды: ls, cd, whoami, rev, head, chown, exit. Unknown -> сообщение об ошибке.
"""
import argparse, json, base64, os, sys, getpass, datetime, xml.etree.ElementTree as ET
from typing import Optional, Dict, Any, List

# -------------------------
# VFS node
# -------------------------
class VNode:
    def __init__(self, name: str, ntype: str, owner: str = "root", mode: str = "rw", content: bytes = b""):
        self.name = name
        assert ntype in ("dir", "file")
        self.type = ntype
        self.owner = owner
        self.mode = mode
        self.content = content
        self.children: Dict[str, 'VNode'] = {}  # only for dir

    def add_child(self, node: 'VNode'):
        if self.type != "dir":
            raise ValueError("cannot add child to file")
        self.children[node.name] = node

    def repr_line(self):
        if self.type == "dir":
            return f"{self.name}/\t<dir>\towner:{self.owner}"
        else:
            return f"{self.name}\t<file>\towner:{self.owner}\tsize:{len(self.content)}"

# -------------------------
# Build VFS from JSON
# -------------------------
def build_vfs_from_json(js: Dict[str,Any]) -> VNode:
    """
    Expected format:
    {
      "name": "myvfs",
      "root": {
         "type": "dir",
         "children": {
             "file.txt": {"type":"file", "content":"SGVsbG8=", "owner":"alice"},
             "dir1": {"type":"dir", "children": {...} }
         }
      }
    }
    """
    if "root" not in js:
        raise ValueError("VFS JSON missing 'root' key")
    def make(name: str, obj: Dict[str,Any]) -> VNode:
        t = obj.get("type")
        if t == "dir":
            node = VNode(name, "dir", owner=obj.get("owner","root"), mode=obj.get("mode","rw"))
            for child_name, child_def in obj.get("children", {}).items():
                node.add_child(make(child_name, child_def))
            return node
        elif t == "file":
            b64 = obj.get("content", "")
            try:
                content = base64.b64decode(b64) if b64 else b""
            except Exception as e:
                raise ValueError(f"bad base64 for file {name}: {e}")
            return VNode(name, "file", owner=obj.get("owner","root"), mode=obj.get("mode","rw"), content=content)
        else:
            raise ValueError(f"invalid node type {t} for {name}")
    root_def = js["root"]
    root = make("/", root_def)
    root.vfs_name = js.get("name", "VFS")
    return root

# -------------------------
# Path resolution utilities
# -------------------------
def split_path(p: str) -> List[str]:
    return [x for x in p.strip().split("/") if x != ""]

def resolve_to_node(root: VNode, cwd_parts: List[str], path: str) -> (Optional[VNode], Optional[str]):
    """
    Returns (node, None) or (None, error_msg).
    Path supports: absolute (/a/b), relative, ., .. .
    """
    if path == "" or path == ".":
        # return current directory node
        cur = root
        for part in cwd_parts:
            if part not in cur.children:
                return None, f"cwd broken: {part}"
            cur = cur.children[part]
        return cur, None
    # build parts list
    if path.startswith("/"):
        parts = split_path(path)
    else:
        parts = cwd_parts + split_path(path)
    # normalize ., ..
    stack = []
    for p in parts:
        if p == "" or p == ".":
            continue
        if p == "..":
            if stack:
                stack.pop()
            continue
        stack.append(p)
    # traverse
    cur = root
    for p in stack:
        if cur.type != "dir":
            return None, f"not a directory: {'/'.join(stack[:-1])}"
        if p not in cur.children:
            return None, f"path not found: {'/' + '/'.join(stack) if stack else '/'}"
        cur = cur.children[p]
    return cur, None

# -------------------------
# XML logging
# -------------------------
def ensure_xml_log(path: str):
    if not os.path.exists(path):
        root = ET.Element("log")
        tree = ET.ElementTree(root)
        tree.write(path, encoding="utf-8", xml_declaration=True)

def append_xml_event(path: str, user: str, command: str, args: List[str]):
    try:
        ensure_xml_log(path)
        tree = ET.parse(path)
        root = tree.getroot()
        ev = ET.SubElement(root, "event")
        ev.set("time", datetime.datetime.utcnow().isoformat() + "Z")
        ev.set("user", user)
        cmd_el = ET.SubElement(ev, "command"); cmd_el.text = command
        args_el = ET.SubElement(ev, "args"); args_el.text = " ".join(args)
        tree.write(path, encoding="utf-8", xml_declaration=True)
    except Exception as e:
        print(f"Error writing log: {e}", file=sys.stderr)

# -------------------------
# Emulator core
# -------------------------
class Emulator:
    def __init__(self, root: VNode, log_path: Optional[str]=None, start_script: Optional[str]=None):
        self.root = root
        self.vfs_name = getattr(root, "vfs_name", "VFS")
        self.cwd_parts: List[str] = []  # empty -> root
        self.log_path = log_path
        self.start_script = start_script
        self.user = getpass.getuser() or "unknown"

    def prompt(self) -> str:
        cur = "/" if not self.cwd_parts else "/" + "/".join(self.cwd_parts)
        return f"[{self.vfs_name}]{cur}$ "

    def log_cmd(self, command: str, args: List[str]):
        if self.log_path:
            append_xml_event(self.log_path, self.user, command, args)

    def run_command(self, command: str, args: List[str]) -> Optional[str]:
        # log
        self.log_cmd(command, args)
        # dispatch
        if command == "exit":
            print("Bye.")
            sys.exit(0)
        if command == "ls":
            return self.cmd_ls(args)
        if command == "cd":
            return self.cmd_cd(args)
        if command == "whoami":
            return self.user
        if command == "rev":
            return self.cmd_rev(args)
        if command == "head":
            return self.cmd_head(args)
        if command == "chown":
            return self.cmd_chown(args)
        return f"Unknown command: {command}"

    # ls: if arg given -> list that path, else list cwd
    def cmd_ls(self, args: List[str]) -> str:
        path = args[0] if args else ""
        node, err = resolve_to_node(self.root, self.cwd_parts, path)
        if node is None:
            return f"ls: {err}"
        if node.type == "file":
            return node.repr_line()
        # directory
        lines = []
        for name in sorted(node.children.keys()):
            lines.append(node.children[name].repr_line())
        return "\n".join(lines)

    # cd: change cwd to path or to root if no args
    def cmd_cd(self, args: List[str]) -> str:
        if len(args) > 1:
            return "cd: too many arguments"
        path = args[0] if args else "/"
        node, err = resolve_to_node(self.root, self.cwd_parts, path)
        if node is None:
            return f"cd: {err}"
        if node.type != "dir":
            return f"cd: not a directory: {path}"
        # set cwd_parts
        if path.startswith("/"):
            self.cwd_parts = split_path(path)
        else:
            parts = self.cwd_parts + split_path(path)
            stack = []
            for p in parts:
                if p == "" or p == ".":
                    continue
                if p == "..":
                    if stack: stack.pop()
                else:
                    stack.append(p)
            self.cwd_parts = stack
        return ""

    # rev: if argument resolves to file -> reverse file content (decoded), else reverse joined args
    def cmd_rev(self, args: List[str]) -> str:
        if not args:
            return ""
        maybe = args[0]
        node, err = resolve_to_node(self.root, self.cwd_parts, maybe)
        if node is not None and node.type == "file":
            try:
                text = node.content.decode(errors="replace")
                return text[::-1]
            except Exception:
                return f"rev: cannot decode {maybe}"
        # else treat as string
        return " ".join(args)[::-1]

    # head: head [-n N] filename
    def cmd_head(self, args: List[str]) -> str:
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
        node, err = resolve_to_node(self.root, self.cwd_parts, filename)
        if node is None:
            return f"head: {err}"
        if node.type != "file":
            return f"head: {filename}: not a file"
        text = node.content.decode(errors="replace").splitlines()
        return "\n".join(text[:n])

    # chown owner path
    def cmd_chown(self, args: List[str]) -> str:
        if len(args) < 2:
            return "chown: usage: chown owner path"
        owner = args[0]
        path = args[1]
        node, err = resolve_to_node(self.root, self.cwd_parts, path)
        if node is None:
            return f"chown: {err}"
        node.owner = owner
        return ""

    # start-script execution
    def run_start_script(self):
        if not self.start_script:
            return
        if not os.path.exists(self.start_script):
            print(f"Start script not found: {self.start_script}")
            return
        print(f"--- Executing start script: {self.start_script} ---")
        try:
            with open(self.start_script, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    stripped = line.strip()
                    if stripped == "" or stripped.startswith("#"):
                        # show comment or blank as-is
                        print(line)
                        continue
                    print(f">>> {line}")
                    parts = stripped.split()
                    cmd = parts[0]
                    args = parts[1:]
                    out = self.run_command(cmd, args)
                    if out is not None and out != "":
                        print(out)
        except Exception as e:
            print(f"Error executing start script: {e}")

    # REPL
    def repl(self):
        # execute start script (if any) then REPL
        self.run_start_script()
        try:
            while True:
                try:
                    line = input(self.prompt())
                except EOFError:
                    print()  # clean exit on Ctrl-D
                    break
                except KeyboardInterrupt:
                    print()  # ignore Ctrl-C, new line
                    continue
                s = line.strip()
                if not s:
                    continue
                parts = s.split()
                cmd = parts[0]
                args = parts[1:]
                out = self.run_command(cmd, args)
                if out is not None and out != "":
                    print(out)
        except SystemExit:
            raise
        except Exception as e:
            print(f"Unexpected error in REPL: {e}", file=sys.stderr)

# -------------------------
# Config loader and CLI
# -------------------------
def load_config(path: str) -> Dict[str, Optional[str]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
            return {
                "vfs": j.get("vfs") or j.get("path_to_vfs"),
                "log": j.get("log"),
                "start": j.get("start")
            }
    except Exception as e:
        print(f"Error reading config file {path}: {e}", file=sys.stderr)
        return {}

def main():
    ap = argparse.ArgumentParser(description="Minimal emulator stages1-5")
    ap.add_argument("--vfs", help="VFS JSON file", default=None)
    ap.add_argument("--log", help="log XML file", default=None)
    ap.add_argument("--start", help="start script file", default=None)
    ap.add_argument("--config", help="config JSON file (overrides CLI)", default=None)
    args = ap.parse_args()

    cli = {"vfs": args.vfs, "log": args.log, "start": args.start}
    cfg = {}
    if args.config:
        cfg = load_config(args.config)
        if cfg == {}:
            print("Warning: config could not be read or empty; continuing with CLI values.", file=sys.stderr)

    # merge: config overrides cli
    final = cli.copy()
    for k,v in (cfg or {}).items():
        if v:
            final[k] = v

    vfs_path = final.get("vfs")
    log_path = final.get("log")
    start_script = final.get("start")

    # load VFS
    if vfs_path:
        if not os.path.exists(vfs_path):
            print(f"Error: VFS file not found: {vfs_path}", file=sys.stderr)
            # fallback empty root
            root = VNode("/", "dir")
            root.vfs_name = "VFS"
        else:
            try:
                with open(vfs_path, "r", encoding="utf-8") as f:
                    j = json.load(f)
                    root = build_vfs_from_json(j)
            except Exception as e:
                print(f"Error loading VFS from {vfs_path}: {e}", file=sys.stderr)
                root = VNode("/", "dir")
                root.vfs_name = "VFS"
    else:
        root = VNode("/", "dir")
        root.vfs_name = "VFS"

    # prepare log file
    if log_path:
        try:
            ensure_dir = os.path.dirname(log_path)
            if ensure_dir and not os.path.exists(ensure_dir):
                os.makedirs(ensure_dir, exist_ok=True)
            # ensure file exists
            if not os.path.exists(log_path):
                root_el = ET.Element("log")
                ET.ElementTree(root_el).write(log_path, encoding="utf-8", xml_declaration=True)
        except Exception as e:
            print(f"Error preparing log file {log_path}: {e}", file=sys.stderr)

    # Debug print (Stage 2 requirement)
    print("=== Emulator starting (debug) ===")
    print(f"VFS file: {vfs_path}")
    print(f"Log file: {log_path}")
    print(f"Start script: {start_script}")
    print(f"Config file used: {args.config}")
    print(f"User: {getpass.getuser()}")
    print("=================================")

    em = Emulator(root, log_path, start_script)
    em.repl()

if __name__ == "__main__":
    main()

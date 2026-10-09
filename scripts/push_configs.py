#!/usr/bin/env python3
import argparse
import difflib
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

try:
    import curses
except ImportError:
    curses = None

if os.name == "nt":
    import msvcrt
else:
    import termios
    import tty

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None

REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "configs"
LOG_DIR = REPO / "logs"

HOSTNAME_RE = re.compile(r"^\s*hostname\s+(\S+)\s*$", re.IGNORECASE | re.MULTILINE)
PROMPT_RE = re.compile(r"(?:^|[\r\n])([\w.\-/]+)(\([\w.\-/ ]+\))?([>#])\s*$")
PROMPT_LINE_RE = re.compile(r"^([\w.\-/]+)(\([\w.\-/ ]+\))?([>#])[ \t]*$", re.MULTILINE)
QUESTION_RE = re.compile(r"(\[yes/no\]|\[confirm\]|\(yes/no\))[:?]?\s*$", re.IGNORECASE)
ERROR_RE = re.compile(r"^\s*%\s*(Invalid|Incomplete|Ambiguous|Unknown|Bad|Unrecognized).*$", re.MULTILINE)
SKIP_RE = re.compile(r"^(!.*|end|Building configuration.*|Current configuration.*)$", re.IGNORECASE)
CONFLICT_RE = re.compile(r"^(<{7}|={7}|>{7})( |$)", re.MULTILINE)
NOTE_RE = re.compile(r"^[A-Z][A-Za-z ]*:(\s|$)")
PKI_RE = re.compile(r"^crypto pki (trustpoint|certificate chain) TP-self-signed", re.IGNORECASE)
DROP_RE = re.compile(r"^(Building configuration.*|Current configuration.*|! Last configuration change.*|"
                     r"! NVRAM config last updated.*|! No configuration change since.*|ntp clock-period.*)$")
SYSLOG_RE = re.compile(r"^\*?([A-Z][a-z]{2} +\d+ [\d:.]+: )?%[A-Z0-9_]+-\d-[A-Z0-9_]+:")
VLAN_LINE_RE = re.compile(r"^(\d+)\s+(\S+)\s+(active|act/lshut|act/unsup|suspended|sus/lshut)", re.MULTILINE)
FACTORY_NAMES = {"switch", "router"}
ESCAPE_KEY = b"\x1d"
SLOW_RE = re.compile(r"^\s*(crypto key|write|copy)\b", re.IGNORECASE)

TODO, BUSY, DONE, WARN, FAIL = "[    ]", "[ .. ]", "[done]", "[warn]", "[FAIL]"
GREEN, RED, YELLOW, CYAN = 1, 2, 3, 4
STATUS_COLOUR = {DONE: GREEN, WARN: YELLOW, FAIL: RED, BUSY: CYAN}
LIST_WIDTH = 26


class DeviceError(Exception):
    pass


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)


def config_lines(path):
    lines = []
    notes = []
    in_pki = False
    for raw in path.read_text(errors="replace").splitlines():
        line = raw.rstrip()
        if in_pki and line.startswith(" "):
            continue
        in_pki = bool(PKI_RE.match(line))
        if in_pki or not line.strip() or SKIP_RE.match(line.strip()):
            continue
        if NOTE_RE.match(line):
            notes.append(line)
            continue
        banner = re.match(r"^(\s*banner \w+ )\^C(.*)\^C$", line, re.IGNORECASE)
        if banner:
            delim = next(d for d in "#$%~@" if d not in banner.group(2))
            line = f"{banner.group(1)}{delim}{banner.group(2)}{delim}"
        lines.append(line)
    return lines, notes


class Device:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.status = TODO
        self.result = ""
        self.pulled = False
        self.refresh()

    def refresh(self):
        rel = str(self.path.relative_to(REPO))
        self.conflict = bool(CONFLICT_RE.search(self.path.read_text(errors="replace")))
        lines, notes = config_lines(self.path)
        skipped = f", {len(notes)} note line(s) skipped" if notes else ""
        info = [f"{self.name}   {rel}", f"{len(lines)} lines to send{skipped}"]
        if self.result:
            info.append(f"This session: {self.result}")
        last = git("log", "-1", "--date=format:%Y-%m-%d %H:%M",
                   "--format=%h%x1f%an%x1f%ad%x1f%ar%x1f%s", "--", rel).stdout.strip()
        if last:
            commit, author, date, ago, subject = last.split("\x1f")
            info += [f"Last changed: {date} ({ago})", f"By:           {author}", f"Commit:       {commit} {subject}"]
        else:
            info.append("Not committed to git yet")
        if self.pulled:
            info.append("* Updated by the pull you just did")
        if git("status", "--porcelain", "--", rel).stdout.strip():
            info.append("! Has local edits that are not committed")
        if self.conflict:
            info.append("! MERGE CONFLICT markers in this file, it cannot be sent")
        diff = [line.rstrip() for line in git("log", "-1", "-p", "--format=", "--", rel).stdout.splitlines()
                if line and line[0] in "+-" and not line.startswith(("+++", "---"))]
        if diff:
            info += ["Lines changed in that commit:", *diff]
        self.info = info


def load_devices():
    devices = []
    seen = {}
    for path in sorted(CONFIG_DIR.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        text = path.read_text(errors="replace")
        match = HOSTNAME_RE.search(text)
        if not match:
            continue
        name = match.group(1)
        if seen.get(name) == text:
            continue
        seen[name] = text
        devices.append((name, path.resolve()))
    return devices


def check_configs():
    in_ci = "GITHUB_ACTIONS" in os.environ
    counts = {"error": 0, "warning": 0}

    def report(level, path, line, message):
        counts[level] += 1
        where = f"{path.relative_to(REPO)}:{line}"
        if in_ci:
            print(f"::{level} file={path.relative_to(REPO)},line={line}::{where}: {message}")
        else:
            print(f"{level.upper():<8}{where}: {message}")

    by_name = {}
    for path in sorted(CONFIG_DIR.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        text = path.read_text(errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            if CONFLICT_RE.match(line):
                report("error", path, number, "unresolved git merge conflict marker")
            elif NOTE_RE.match(line) and not SKIP_RE.match(line.strip()):
                report("warning", path, number, "looks like a note, not a command (start it with ! to keep it)")
        match = HOSTNAME_RE.search(text)
        if not match:
            report("warning", path, 1, "no hostname line, so this file is never sent to a device")
            continue
        name = match.group(1)
        if name in by_name and by_name[name].read_text(errors="replace") != text:
            report("error", path, 1, f"second, different config for {name} "
                                     f"(also {by_name[name].relative_to(REPO)})")
        elif name in by_name:
            report("warning", path, 1, f"identical copy of {by_name[name].relative_to(REPO)}")
        by_name.setdefault(name, path)

    print(f"\n{len(by_name)} device configs, {counts['error']} error(s), {counts['warning']} warning(s).")
    return counts["error"] == 0


def split_reply(text, sent=""):
    text = text.replace("\r", "")
    echo = text.find(sent) if sent else -1
    rest = text[echo + len(sent):] if echo >= 0 else text.partition("\n")[2]
    found = list(PROMPT_LINE_RE.finditer(rest))
    return rest, found[-1] if found else None


def prompt_after(text, sent=""):
    return split_reply(text, sent)[1]


def reply_body(text, sent):
    rest, prompt = split_reply(text, sent)
    return (rest[:prompt.start()] if prompt else rest).lstrip("\n")


def is_show_run(text):
    lines = [line.strip() for line in text.replace("\r", "").splitlines() if line.strip()]
    return bool(lines) and (lines[-1] == "end" or any(
        line.startswith(("Building configuration", "Current configuration")) for line in lines[:5]))


def clean_capture(raw):
    lines = []
    in_pki = False
    for line in raw.splitlines():
        line = line.rstrip()
        if SYSLOG_RE.match(line) or (in_pki and line.startswith(" ")):
            continue
        in_pki = bool(PKI_RE.match(line))
        if in_pki or DROP_RE.match(line):
            continue
        lines.append(line)
    while lines and lines[0] in ("", "!"):
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def restore_dropped(lines, old_text, vlans):
    text = "\n".join(lines)
    if not re.search(r"^\s*crypto key generate rsa", text, re.IGNORECASE | re.MULTILINE):
        old = re.search(r"crypto key generate rsa\b.*?modulus\s+(\d+)", old_text or "", re.IGNORECASE)
        key = f"crypto key generate rsa modulus {old.group(1) if old else 2048}"
        at = next((i for i, line in enumerate(lines) if re.match(r"ip domain[ -]name ", line, re.IGNORECASE)), None)
        if at is None:
            at = next((i for i, line in enumerate(lines) if HOSTNAME_RE.match(line)), -1)
        lines.insert(at + 1, key)
    have = {int(number) for number in re.findall(r"^vlan (\d+)$", text, re.MULTILINE)}
    block = []
    for number, name in vlans:
        if number in have:
            continue
        block.append(f"vlan {number}")
        if name != f"VLAN{number:04d}":
            block.append(f" name {name}")
        block.append("!")
    if block:
        at = next((i for i, line in enumerate(lines) if line.startswith("interface ")), len(lines))
        lines[at:at] = block
    return lines


def capture(console):
    console.command("terminal length 0")
    console.command("terminal width 512")
    output, finished = console.command("show running-config", timeout=180)
    if not finished:
        raise DeviceError("the device did not finish sending its running config")
    config = clean_capture(reply_body(output, "show running-config"))
    if not any(HOSTNAME_RE.match(line) for line in config):
        raise DeviceError("the captured config has no hostname line, so it looks incomplete")
    vlan_output, _ = console.command("show vlan brief", timeout=30)
    vlans = []
    if not ERROR_RE.search(vlan_output):
        for match in VLAN_LINE_RE.finditer(reply_body(vlan_output, "show vlan brief")):
            number = int(match.group(1))
            if number != 1 and not 1002 <= number <= 1005:
                vlans.append((number, match.group(2)))
    return config, vlans


def console_session(port, log):
    port.write(b"\r")
    if os.name == "nt":
        while True:
            if port.in_waiting:
                data = port.read(port.in_waiting)
                sys.stdout.write(data.decode("ascii", errors="replace"))
                sys.stdout.flush()
                log.write(data.decode("ascii", errors="replace"))
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    msvcrt.getwch()
                elif key.encode() == ESCAPE_KEY:
                    return
                else:
                    port.write(key.encode("ascii", errors="ignore"))
            else:
                time.sleep(0.01)
    keyboard = sys.stdin.fileno()
    saved = termios.tcgetattr(keyboard)
    tty.setraw(keyboard)
    try:
        while True:
            ready = select.select([keyboard, port.fileno()], [], [], 0.2)[0]
            if port.fileno() in ready:
                data = port.read(port.in_waiting or 1)
                os.write(sys.stdout.fileno(), data)
                log.write(data.decode("ascii", errors="replace"))
            if keyboard in ready:
                data = os.read(keyboard, 1024)
                if ESCAPE_KEY in data:
                    port.write(data.split(ESCAPE_KEY)[0])
                    return
                port.write(data)
    finally:
        termios.tcsetattr(keyboard, termios.TCSADRAIN, saved)


class Console:
    def __init__(self, port, log, ui):
        self.port = port
        self.log = log
        self.ui = ui
        self.last_prompt = ""

    def read(self, timeout, idle, done=None):
        buf = ""
        start = last = time.monotonic()
        while True:
            chunk = self.port.read(self.port.in_waiting or 1)
            now = time.monotonic()
            if chunk:
                text = chunk.decode("ascii", errors="replace")
                self.log.write(text)
                self.ui.stream(text)
                buf += text
                last = now
            elif buf and now - last >= idle and (done is None or done(buf)):
                self.ui.draw()
                return buf, True
            if now - start > timeout:
                self.ui.draw()
                return buf, False

    def write(self, text):
        self.port.write((text + "\r").encode("ascii", errors="replace"))
        self.port.flush()

    def command(self, line, timeout=8):
        self.write(line)
        sent = line.strip()
        output = ""
        for _ in range(4):
            text, finished = self.read(timeout, 0.02, lambda b: "\n" in b and (
                QUESTION_RE.search(b) or prompt_after(b, sent)))
            output += text
            prompt = prompt_after(output, sent)
            if prompt:
                self.last_prompt = prompt.group(0).strip()
            if not finished or not QUESTION_RE.search(text):
                return output, finished
            self.write("yes" if "yes/no" in text.lower()[-12:] else "")
        return output, False

    def login(self):
        self.port.reset_input_buffer()
        self.write("")
        silent = 0
        patience = 3
        secret = None
        used_saved_password = False
        for _ in range(40):
            out, _ = self.read(4, 1.0)
            prompt = PROMPT_RE.search(out)
            tail = out.strip().splitlines()[-1].strip() if out.strip() else ""

            if not out.strip():
                silent += 1
                if silent >= patience:
                    raise DeviceError("no response from the device. Check the cable is in the CONSOLE "
                                      "port and the device is powered on and finished booting")
                self.write("")
            elif re.search(r"initial configuration dialog\? \[yes/no\]:\s*$", out):
                self.ui.say("Fresh device: declining the initial configuration dialog (this can take a minute).")
                self.write("no")
                silent, patience = 0, 15
            elif re.search(r"terminate autoinstall\? \[yes\]:\s*$", out):
                self.ui.say("Terminating autoinstall.")
                self.write("yes")
                silent, patience = 0, 15
            elif re.search(r"enable secret:\s*$", tail, re.IGNORECASE):
                if secret is None:
                    secret = self.ui.ask("Device insists on an enable secret before continuing: ", secret=True)
                self.write(secret)
            elif re.search(r"Enter your selection.*:\s*$", tail):
                self.write("0")
            elif re.search(r"username:\s*$", tail, re.IGNORECASE):
                self.write(self.ui.ask("Device asks for a username: "))
            elif re.search(r"password:\s*$", tail, re.IGNORECASE):
                if self.ui.password is None or used_saved_password:
                    self.ui.password = self.ui.ask("Device asks for a password: ", secret=True)
                    used_saved_password = False
                else:
                    used_saved_password = True
                self.write(self.ui.password)
            elif prompt and prompt.group(2):
                self.write("end")
            elif prompt and prompt.group(3) == ">":
                self.write("enable")
            elif prompt:
                return prompt.group(1)
            else:
                self.write("")
        raise DeviceError("could not reach an enable (#) prompt")


def push(console, device):
    ui = console.ui
    lines, notes = config_lines(device.path)
    problems = []
    misses = 0
    for note in notes:
        ui.say(f"Not sending, looks like a note rather than a command: {note.strip()}")

    def send(line, progress):
        nonlocal misses
        ui.progress = f"{device.name}: {progress}"
        output, finished = console.command(line, timeout=90 if SLOW_RE.match(line) else 8)
        error = ERROR_RE.search(output)
        if error:
            problems.append((line, error.group(0).strip()))
            ui.say(f"REJECTED: {line.strip()}", RED)
        elif not finished:
            ui.say("No prompt came back, carrying on.", YELLOW)
            console.write("")
            answer = console.read(3, 0.3)[0]
            output += answer
            if not prompt_after(answer):
                misses += 1
                if misses >= 3:
                    raise DeviceError(f"the device stopped responding at '{line.strip()}'. Its config is only "
                                      "partly applied")
                return output
        misses = 0
        return output

    send("terminal length 0", "setting up")
    send("terminal width 512", "setting up")
    send("configure terminal", "setting up")
    ui.say("Press t at any time to pause and type on the device's console yourself.")
    for number, line in enumerate(lines, 1):
        if ui.pause_requested() and not ui.take_over(console, f"before line {number} of {len(lines)}"):
            raise DeviceError(f"you stopped it before line {number} of {len(lines)}, so the config is only "
                              "partly applied")
        output = send(line, f"line {number} of {len(lines)}")
        prompt = prompt_after(output, line.strip())
        if prompt and not prompt.group(2):
            send("configure terminal", "setting up")
    send("end", "finishing")

    if ui.ask_yes(f"Save to startup-config on {device.name} (write memory)?"):
        send("write memory", "saving")
    return problems


class UI:
    def __init__(self, screen, devices, known_names, args):
        self.screen = screen
        self.devices = devices
        self.known_names = known_names
        self.args = args
        self.selected = 0
        self.output = [("", 0)]
        self.scroll = 0
        self.progress = ""
        self.drawn = 0.0
        self.port = None
        self.password = None
        curses.curs_set(0)
        if curses.has_colors():
            curses.use_default_colors()
            for pair, colour in ((GREEN, curses.COLOR_GREEN), (RED, curses.COLOR_RED),
                                 (YELLOW, curses.COLOR_YELLOW), (CYAN, curses.COLOR_CYAN)):
                curses.init_pair(pair, colour, -1)


    def say(self, text="", colour=CYAN):
        partial = self.output.pop()
        width = max(20, self.screen.getmaxyx()[1] - 4)
        for line in str(text).splitlines() or [""]:
            for part in textwrap.wrap(line, width, drop_whitespace=False) or [""]:
                self.output.append((f">> {part}", colour))
        self.output.append(partial)
        self.draw()

    def stream(self, text):
        for char in text.replace("\r", ""):
            if char == "\n":
                self.output.append(("", 0))
            elif char == "\b":
                self.output[-1] = (self.output[-1][0][:-1], 0)
            elif char.isprintable():
                self.output[-1] = (self.output[-1][0] + char, self.output[-1][1])
        del self.output[:-5000]
        if time.monotonic() - self.drawn > 0.05:
            self.draw()


    def put(self, row, col, text, attr=0, width=None):
        height, full = self.screen.getmaxyx()
        width = min(width if width is not None else full - col, full - col)
        if row >= height or width <= 0:
            return
        try:
            self.screen.addnstr(row, col, text.ljust(width) if attr & curses.A_REVERSE else text, width, attr)
        except curses.error:
            pass

    def draw(self, prompt=None):
        self.drawn = time.monotonic()
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 14 or width < 70:
            self.put(0, 0, "Make this window bigger (at least 70x14).")
            self.screen.refresh()
            return
        top = min(len(self.devices), max(5, (height - 3) * 45 // 100))

        cable = f"{self.port.port} @ {self.port.baudrate}" if self.port else "not found yet"
        title = f" Console config push   cable: {cable}   {self.progress}"
        self.put(0, 0, title, curses.A_REVERSE, width)

        first = max(0, min(self.selected - top + 1, len(self.devices) - top))
        for row, device in enumerate(self.devices[first:first + top], 1):
            chosen = device is self.devices[self.selected]
            status = "[conf]" if device.conflict and device.status == TODO else device.status
            colour = curses.color_pair(RED if status == "[conf]" else STATUS_COLOUR.get(status, 0))
            mark = "*" if device.pulled else " "
            self.put(row, 0, f" {status}", colour | (curses.A_REVERSE if chosen else 0), 8)
            self.put(row, 8, f"{mark}{device.name}", curses.A_REVERSE if chosen else 0, LIST_WIDTH - 8)

        for row, line in enumerate(self.devices[self.selected].info[:top], 1):
            colour = {"+": GREEN, "-": RED, "!": YELLOW, "*": CYAN}.get(line[:1], 0)
            self.put(row, LIST_WIDTH + 1, line, curses.color_pair(colour) | (curses.A_BOLD if row == 1 else 0))

        back = f" (scrolled back {self.scroll}, PgDn to return)" if self.scroll else ""
        self.put(top + 1, 0, f" Console output{back} ".center(width, "-"), curses.A_DIM)
        rows = height - top - 3
        end = len(self.output) - self.scroll
        for row, (line, colour) in enumerate(self.output[max(0, end - rows):end], top + 2):
            self.put(row, 0, line, curses.color_pair(colour))

        if prompt is None:
            self.put(height - 1, 0, " Up/Down select   Enter send config   g grab config   t terminal   p pull   "
                                    "c cable   PgUp/PgDn scroll   q quit", curses.A_REVERSE, width)
        else:
            self.put(height - 1, 0, prompt[-(width - 1):], curses.A_BOLD)
        self.screen.refresh()

    def scroll_output(self, key):
        page = max(1, self.screen.getmaxyx()[0] // 3)
        if key == curses.KEY_PPAGE:
            self.scroll = min(self.scroll + page, max(0, len(self.output) - page))
        elif key == curses.KEY_NPAGE:
            self.scroll = max(0, self.scroll - page)


    def ask(self, prompt, secret=False):
        typed = ""
        curses.flushinp()
        while True:
            self.draw(f" {prompt}{'*' * len(typed) if secret else typed}_")
            key = self.screen.get_wch()
            if key in ("\n", "\r", curses.KEY_ENTER):
                return typed.strip()
            if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                typed = typed[:-1]
            elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                self.scroll_output(key)
            elif isinstance(key, str) and key.isprintable():
                typed += key

    def ask_yes(self, prompt, default=True):
        answer = self.ask(f"{prompt} [{'Y/n' if default else 'y/N'}] ").lower()
        return default if not answer else answer.startswith("y")

    def suspend(self, message, action):
        curses.def_prog_mode()
        curses.endwin()
        print(f"\n{message}", flush=True)
        try:
            return action()
        except KeyboardInterrupt:
            return None
        finally:
            curses.reset_prog_mode()
            self.screen.clearok(True)
            self.draw()


    def pull(self):
        branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not self.ask_yes(f"Pull the latest configs from git (branch {branch})?"):
            self.say("Not pulling, using the configs already on this laptop.")
            return
        before = git("rev-parse", "HEAD").stdout.strip()
        while True:
            result = self.suspend("Running git pull...", lambda: git("pull", "--ff-only"))
            if result and result.returncode == 0:
                break
            self.say("git pull failed:", RED)
            self.say((result.stderr or result.stdout).strip() if result else "cancelled with Ctrl-C", RED)
            if not self.ask("[r]etry, or Enter to continue with the configs already here: ").lower().startswith("r"):
                return
        after = git("rev-parse", "HEAD").stdout.strip()
        if before == after:
            self.say("Already up to date, nothing changed.")
            return

        self.say(f"Updated {before[:7]} -> {after[:7]}. Commits:")
        self.say(git("log", "--format=  %h  %an, %ar: %s", f"{before}..{after}").stdout.rstrip())
        names = git("diff", "--name-only", before, after, "--", "configs").stdout.split("\n")
        changed = {(REPO / name).resolve() for name in names if name}
        self.reload_devices(changed)
        updated = [device.name for device in self.devices if device.path in changed]
        self.say(f"Configs changed (marked *): {', '.join(updated)}" if updated
                 else "No device configs changed in this update.")

    def reload_devices(self, changed=frozenset()):
        old = {device.path: device for device in self.devices}
        wanted = {name.lower() for name in self.args.only or []}
        self.devices = []
        for name, path in load_devices():
            if wanted and name.lower() not in wanted:
                continue
            device = old.get(path) or Device(name, path)
            device.name = name
            device.pulled = device.pulled or path in changed
            if device.pulled and device.status == DONE:
                device.status, device.result = TODO, "sent earlier, but the config has changed since"
            device.refresh()
            self.devices.append(device)
        self.known_names = {name for name, _ in load_devices()}
        self.selected = max(0, min(self.selected, len(self.devices) - 1))

    def pause_requested(self):
        self.screen.nodelay(True)
        wanted = False
        try:
            while True:
                key = self.screen.getch()
                if key == -1:
                    return wanted
                if key in (ord("t"), ord("T")):
                    wanted = True
        finally:
            self.screen.nodelay(False)

    def terminal(self, log):
        self.suspend(f"Connected to the console on {self.port.port}. Type as you would in any terminal.\n"
                     "Press Ctrl-] to go back to the tool.\n",
                     lambda: console_session(self.port, log))
        self.say("Back from the console.")

    def take_over(self, console, where):
        before = console.last_prompt
        self.say(f"Paused {where}. The device was at '{before}'.", YELLOW)
        self.terminal(console.log)
        console.port.reset_input_buffer()
        console.command("")
        now = console.last_prompt
        if now == before:
            self.say(f"Resuming at '{now}'.")
            return True
        self.say(f"The device is now at '{now}', not '{before}' where the tool left it.", YELLOW)
        choice = self.ask("[c] return to config mode and resume, [r] resume as it is, or [a] abort: ").lower()
        if choice.startswith("c"):
            console.command("end")
            console.command("configure terminal")
            return True
        return choice.startswith("r")

    def open_terminal(self):
        if not self.port:
            self.find_cable()
            if not self.port:
                return
        LOG_DIR.mkdir(exist_ok=True)
        with open(LOG_DIR / f"terminal-{time.strftime('%Y%m%d-%H%M%S')}.log", "w", encoding="utf-8") as log:
            self.terminal(log)

    def show_diff(self, old_lines, new_lines, path):
        diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))[2:]
        added = sum(1 for line in diff if line.startswith("+"))
        removed = sum(1 for line in diff if line.startswith("-"))
        self.say(f"Changes to {path.relative_to(REPO)}: {added} line(s) added, {removed} removed "
                 "(PgUp/PgDn to scroll)")
        for line in diff:
            colour = GREEN if line.startswith("+") else RED if line.startswith("-") else 0
            self.output.insert(-1, (line if not line.startswith("@@") else "  ...", colour))
        self.draw()
        return bool(diff)

    def edit(self, lines):
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if not editor:
            editor = "notepad" if os.name == "nt" else next(
                (name for name in ("nano", "vim", "vi") if shutil.which(name)), None)
        if not editor:
            self.say("No text editor found. Set the EDITOR environment variable and try again.", RED)
            return lines
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        try:
            self.suspend(f"Opening the config in {editor}. Save and close the editor to come back.",
                         lambda: subprocess.run([*shlex.split(editor), handle.name], check=False))
            return Path(handle.name).read_text(encoding="utf-8").rstrip("\n").splitlines()
        finally:
            os.unlink(handle.name)

    def grab(self):
        if not self.port:
            self.find_cable()
            if not self.port:
                return
        self.scroll = 0
        if self.ask("Plug the console cable into the CONSOLE port of the device to grab the config from, "
                    "then press Enter ([c]ancel): ").lower().startswith("c"):
            return
        self.say("--- grabbing running config ---")
        LOG_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            with open(LOG_DIR / f"grab-{stamp}.log", "w", encoding="utf-8") as log:
                console = Console(self.port, log, self)
                hostname = console.login()
                self.say(f"Connected to '{hostname}', reading its running config...")
                config, vlans = capture(console)
        except DeviceError as err:
            self.say(f"Could not grab the config: {err}.", RED)
            return
        except (serial.SerialException, OSError) as err:
            self.say(f"Lost the console cable: {err}", RED)
            self.port = None
            return
        except KeyboardInterrupt:
            self.say("Cancelled with Ctrl-C.", YELLOW)
            return
        finally:
            self.progress = ""

        path = next((path for name, path in load_devices() if name.lower() == hostname.lower()), None)
        if path is None:
            path = self.new_device_path(hostname)
            if path is None:
                return
        old_text = path.read_text(errors="replace") if path.exists() else ""
        config = restore_dropped(config, old_text, vlans)
        old_lines = [line.rstrip() for line in old_text.replace("\r", "").splitlines()]
        converting = bool(old_text) and not is_show_run(old_text)
        if converting:
            self.say(f"{path.relative_to(REPO)} is written by hand in a different style. Saving will convert "
                     "the whole file to the device's running-config style.", YELLOW)

        while True:
            if not self.show_diff(old_lines, config, path):
                self.say(f"{path.relative_to(REPO)} already matches the device. Nothing to save.", GREEN)
                return
            action = "convert and save" if converting else "save"
            choice = self.ask(f"[s] {action}, [e] edit it first, or [d] discard: ").lower()
            if choice.startswith("e"):
                config = self.edit(config)
            elif choice.startswith("s"):
                break
            elif choice.startswith("d"):
                self.say("Discarded, nothing was saved.")
                return

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(config) + "\n", encoding="utf-8")
        self.say(f"Saved {path.relative_to(REPO)}. It is not committed: review it with git diff and put it "
                 "through a pull request.", GREEN)
        if git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main":
            self.say("You are on main, which cannot be pushed to directly. Create a branch before committing.",
                     YELLOW)
        self.reload_devices()

    def new_device_path(self, hostname):
        if hostname.lower() in FACTORY_NAMES:
            self.say(f"This device still has the factory name '{hostname}'. Set its hostname first, then grab "
                     "it again.", RED)
            return None
        if not self.ask_yes(f"There is no config file for {hostname}. Add it as a new device?"):
            return None
        folders = sorted(path.name for path in CONFIG_DIR.iterdir() if path.is_dir())
        self.say(f"Site folders: {', '.join(folders)}")
        folder = self.ask("Which site folder does it belong in? (type a new name to create one): ")
        if not re.fullmatch(r"[\w.-]+", folder):
            self.say("Cancelled, no folder given.")
            return None
        path = CONFIG_DIR / folder / f"{hostname}.txt"
        if not self.ask_yes(f"Create {path.relative_to(REPO)}?"):
            return None
        return path

    def open_port(self, name):
        try:
            return serial.Serial(name, baudrate=self.args.baud, timeout=0.1)
        except (serial.SerialException, OSError) as err:
            denied = getattr(err, "errno", None) == 13 or "ermission" in str(err)
            if not denied or os.name != "posix":
                self.say(f"Could not open {name}: {err}", RED)
                return None
        self.say(f"Your user is not allowed to use {name}. Asking sudo for administrator rights...", YELLOW)
        user = os.environ.get("USER", "your user")
        result = self.suspend(
            f"Your user is not allowed to use {name}, so administrator rights are needed.\n"
            f"This runs: sudo chmod a+rw {name}   (lasts until the cable is unplugged)\n"
            "Press Ctrl-C to skip.\n",
            lambda: subprocess.run(["sudo", "-p", f"sudo password for {user}: ", "chmod", "a+rw", name],
                                   check=False))
        if result is None or result.returncode != 0:
            self.say("Did not get administrator rights, so the cable cannot be used yet.", RED)
        else:
            self.say(f"sudo opened up {name} (it does not ask for your password again if you used sudo in "
                     "this terminal in the last few minutes).")
            try:
                return serial.Serial(name, baudrate=self.args.baud, timeout=0.1)
            except (serial.SerialException, OSError) as err:
                self.say(f"Still could not open {name}: {err}", RED)
        self.say(f"To fix this for good, run: sudo usermod -aG uucp {user}   (the group is 'dialout' on "
                 "Debian, Ubuntu and Fedora), then log out and back in.")
        return None

    def find_cable(self):
        if self.port:
            self.port.close()
            self.port = None
        self.say("Plug the USB end of the console cable into this laptop.")
        self.ask("Press Enter to search for the cable... ")
        while True:
            ports = sorted((p for p in list_ports.comports() if p.hwid != "n/a"), key=lambda p: p.device)
            if not ports:
                self.say("No console cable found.", YELLOW)
                choice = self.ask("Enter to search again, type a port path (e.g. /dev/ttyUSB0 or COM3), "
                                  "or [c]ancel: ")
            else:
                for number, port in enumerate(ports, 1):
                    self.say(f"  {number}) {port.device}  {port.description}")
                default = " (Enter = 1)" if len(ports) == 1 else ""
                choice = self.ask(f"Pick a number{default}, [r] to search again, type a port path, or [c]ancel: ")
                if not choice and len(ports) == 1:
                    choice = "1"
            if choice.lower() in ("", "r", "retry"):
                continue
            if choice.lower() in ("c", "cancel", "q"):
                return
            name = ports[int(choice) - 1].device if choice.isdigit() and 0 < int(choice) <= len(ports) else choice
            self.port = self.open_port(name)
            if self.port:
                self.say(f"Using {name} at {self.args.baud} 8N1.", GREEN)
                return

    def send_selected(self):
        device = self.devices[self.selected]
        device.refresh()
        if device.conflict:
            self.say(f"{device.name}: this file has unresolved git merge conflict markers, so it holds two "
                     "versions of the config. It has to be fixed in git before it can be sent.", RED)
            return
        if not self.port:
            self.find_cable()
            if not self.port:
                return
        self.scroll = 0
        if self.ask(f"Plug the console cable into the CONSOLE port of {device.name}, then press Enter "
                    "([c]ancel): ").lower().startswith("c"):
            return

        device.status = BUSY
        self.say(f"--- {device.name} ---")
        LOG_DIR.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            with open(LOG_DIR / f"{device.name}-{stamp}.log", "w", encoding="utf-8") as log:
                console = Console(self.port, log, self)
                current = console.login()
                self.say(f"Connected, the device currently calls itself '{current}'.")
                wrong_device = current != device.name and current in self.known_names
                if wrong_device and not self.ask_yes(
                        f"That looks like {current}, not {device.name}. Send {device.name}'s config anyway?",
                        default=False):
                    device.status = TODO
                    self.say("Nothing sent.")
                    return
                problems = push(console, device)
        except DeviceError as err:
            device.status, device.result = FAIL, f"FAILED: {err}"
        except (serial.SerialException, OSError) as err:
            device.status, device.result = FAIL, "FAILED: lost the console cable part way through"
            self.say(f"Lost the console cable: {err}", RED)
            self.port = None
        except KeyboardInterrupt:
            device.status, device.result = FAIL, "FAILED: cancelled with Ctrl-C, config may be partly applied"
        else:
            if problems:
                device.status, device.result = WARN, f"sent, but {len(problems)} line(s) were rejected"
                for line, error in problems:
                    self.say(f"rejected: {line.strip()}   ({error})", YELLOW)
            else:
                device.status, device.result = DONE, "config sent with no errors"
        finally:
            self.progress = ""

        colour = {DONE: GREEN, WARN: YELLOW}.get(device.status, RED)
        self.say(f"{device.name}: {device.result}", colour)
        device.refresh()
        if device.status == DONE and self.selected < len(self.devices) - 1:
            self.selected += 1

    def run(self):
        self.say("Select a device and press Enter to send its config over the console cable.")
        if not self.args.no_pull:
            self.pull()
        while True:
            self.draw()
            key = self.screen.get_wch()
            if key in (curses.KEY_UP, "k"):
                self.selected = max(0, self.selected - 1)
            elif key in (curses.KEY_DOWN, "j"):
                self.selected = min(len(self.devices) - 1, self.selected + 1)
            elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                self.scroll_output(key)
            elif key in ("\n", "\r", curses.KEY_ENTER):
                self.send_selected()
            elif key == "g":
                self.grab()
            elif key == "t":
                self.open_terminal()
            elif key == "p":
                self.pull()
            elif key == "c":
                self.find_cable()
            elif key == "q" and self.ask_yes("Quit?"):
                return


def run(args):
    if args.check:
        return check_configs()

    found = load_devices()
    known_names = {name for name, _ in found}
    if args.only:
        wanted = {name.lower() for name in args.only}
        found = [d for d in found if d[0].lower() in wanted]
    if not found:
        sys.exit("No device configs found.")

    if args.dry_run:
        for name, path in found:
            print(f"\n=== {name}  ({path.relative_to(REPO)}) ===")
            lines, notes = config_lines(path)
            print("\n".join(lines))
            for note in notes:
                print(f"(not sent, looks like a note: {note.strip()})")
        return True

    if serial is None:
        sys.exit("pyserial is not installed. Start the script with scripts/run.sh (scripts\\run.bat on Windows).")
    if curses is None:
        sys.exit("The screen library is missing. Start the script with scripts\\run.bat on Windows.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        sys.exit("Run this in a terminal window.")

    devices = [Device(name, path) for name, path in found]
    app = None

    def start(screen):
        nonlocal app
        app = UI(screen, devices, known_names, args)
        app.run()

    try:
        curses.wrapper(start)
    except KeyboardInterrupt:
        pass
    finally:
        if app and app.port:
            app.port.close()
        touched = [device for device in (app.devices if app else []) if device.result]
        if touched:
            print("Summary")
            for device in touched:
                print(f"  {device.name:<14} {device.result}")
            print(f"Console transcripts are in {LOG_DIR.relative_to(REPO)}/")
    return all(device.status == DONE for device in touched)


def main():
    parser = argparse.ArgumentParser(description="Push repo configs to Cisco devices over a console cable.")
    parser.add_argument("--baud", type=int, default=9600, help="console speed (default 9600)")
    parser.add_argument("--no-pull", action="store_true", help="do not offer to git pull at startup")
    parser.add_argument("--only", nargs="+", metavar="DEVICE", help="only list these hostnames")
    parser.add_argument("--dry-run", action="store_true", help="print the lines that would be sent and exit")
    parser.add_argument("--check", action="store_true", help="check the config files for problems and exit")
    sys.exit(0 if run(parser.parse_args()) else 1)


if __name__ == "__main__":
    main()

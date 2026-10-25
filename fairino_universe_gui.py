#!/usr/bin/env python3
"""
fairino_universe_gui.py

Small Tkinter launcher for `ros2 launch fairino_universe fairino_universe.launch.py`.

Lets you pick (partial configuration):
  - robot_model        (fairino3 / 5 / 10 / 16 / 20 / 30)
  - gripper            (none, dh_ag95, or type another name)
  - robot_ip_address   (validated IPv4)

Runs on:
  - Linux / WSL: launches ros2 directly.
  - Windows:     launches ros2 inside WSL via wsl.exe (your default WSL distro,
                 unless you name a specific one).

When the IP differs from the one the hardware plugin was last built with,
`rebuild_plugin:=true` is passed automatically (you can override the checkbox).

Settings are saved to ~/.config/fairino_universe_gui/config.json (Linux) or
%APPDATA%\\fairino_universe_gui\\config.json (Windows).
Launch output is streamed into the window; Stop sends Ctrl+C (SIGINT) to the
launch, escalating to SIGTERM / SIGKILL if it doesn't exit.

Requires: Tkinter (Linux: sudo apt install -y python3-tk; Windows: included
with the python.org installer).
"""
import ipaddress
import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import threading
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------
# Constants (match `ros2 launch fairino_universe fairino_universe.launch.py --show-args`)
# --------------------------------------------------------------------------
PACKAGE = "fairino_universe"
LAUNCH_FILE = "fairino_universe.launch.py"
ROS_SETUP = "/opt/ros/humble/setup.bash"

ROBOT_MODELS = ["fairino3", "fairino5", "fairino10", "fairino16", "fairino20", "fairino30"]
GRIPPERS = ["none", "dh_ag95"]


def _config_dir() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "fairino_universe_gui"
    return Path.home() / ".config" / "fairino_universe_gui"


CONFIG_PATH = _config_dir() / "config.json"
DEFAULTS = {
    "robot_model": "fairino5",
    "gripper": "none",
    "robot_ip_address": "192.168.58.2",
    # Linux path (inside WSL on Windows); a leading ~ is expanded by bash
    "workspace": "~/github/fairino-universe",
    # IP the hardware plugin was last built with (CONTROLLER_IP macro patch)
    "last_built_ip": "192.168.58.2",
    # Windows only: WSL distro that has ROS 2 + the workspace ("" = WSL default distro)
    "wsl_distro": "",
}

MAX_LOG_LINES = 5000
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # hide console windows on Windows


# --------------------------------------------------------------------------
# Helpers (no Tk dependency, easy to test)
# --------------------------------------------------------------------------
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def validate_ip(text: str):
    """Return None if `text` is a usable IPv4 address, else an error message."""
    text = text.strip()
    if not text:
        return "IP address is empty"
    try:
        ip = ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError:
        return "Not a valid IPv4 address"
    if ip.is_unspecified or ip.is_multicast or ip.is_loopback:
        return "Not a usable controller address"
    return None


def build_launch_args(robot_model: str, gripper: str, ip: str, rebuild: bool) -> list:
    return [
        f"robot_model:={robot_model}",
        f"gripper:={gripper.strip() or 'none'}",
        f"robot_ip_address:={ip.strip()}",
        f"rebuild_plugin:={'true' if rebuild else 'false'}",
    ]


def sh_path(p: str) -> str:
    """Quote a Linux path for bash, keeping a leading ~ expandable."""
    p = p.strip()
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def build_shell_command(workspace: str, launch_args: list, pid_file: str = None) -> str:
    """Bash script that sources ROS + the workspace and execs ros2 launch.

    With `pid_file`, the launch PID is written there first (exec keeps the PID),
    so it can be signalled later from a separate WSL call.
    """
    ws = workspace.strip().rstrip("/") or "/"
    launch = " ".join(shlex.quote(a) for a in ["ros2", "launch", PACKAGE, LAUNCH_FILE, *launch_args])
    parts = []
    if pid_file:
        parts.append(f"echo $$ > {shlex.quote(pid_file)}")
    parts += [
        "export PYTHONUNBUFFERED=1 RCUTILS_COLORIZED_OUTPUT=0 RCUTILS_LOGGING_BUFFERED_STREAM=0",
        f"cd {sh_path(ws)}",
        f"source {shlex.quote(ROS_SETUP)}",
        f"source {sh_path(ws + '/install/setup.bash')}",
        f"exec {launch}",
    ]
    return " && ".join(parts)


# ---------------- Windows / WSL helpers ----------------
def wsl_cmd(distro: str, script: str) -> list:
    # -e runs bash directly (no extra login shell re-parsing our script)
    argv = ["wsl.exe"]
    if distro.strip():
        argv += ["-d", distro.strip()]  # blank -> WSL's default distro
    return argv + ["-e", "bash", "-c", script]


def _decode_wsl_output(raw: bytes) -> str:
    # wsl.exe's own messages are UTF-16LE; Linux program output is UTF-8
    if b"\x00" in raw:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def list_wsl_distros() -> list:
    if not IS_WINDOWS:
        return []
    try:
        out = subprocess.run(["wsl.exe", "-l", "-q"], capture_output=True,
                             timeout=15, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = [n.strip().strip("\ufeff") for n in _decode_wsl_output(out).splitlines()]
    return [n for n in names if n]


def default_wsl_distro() -> str:
    """Name of the default WSL distro ('' if it can't be determined)."""
    if not IS_WINDOWS:
        return ""
    try:
        out = subprocess.run(["wsl.exe", "-l"], capture_output=True,
                             timeout=15, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in _decode_wsl_output(out).splitlines():
        if "(default)" in line.lower():
            return re.sub(r"\s*\((?i:default)\)\s*", "", line).strip().strip("\ufeff")
    return ""


def wsl_missing_files(distro: str, path_exprs: list) -> list:
    """Return indices of `path_exprs` (bash-quoted) that don't exist in the distro.

    Raises RuntimeError if the distro can't be reached.
    """
    script = "; ".join(f"test -f {e} || echo MISSING:{i}" for i, e in enumerate(path_exprs))
    script += "; echo DONE"
    try:
        res = subprocess.run(wsl_cmd(distro, script), capture_output=True,
                             timeout=60, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Couldn't run wsl.exe: {exc}")
    out = _decode_wsl_output(res.stdout)
    if "DONE" not in out:
        err = _decode_wsl_output(res.stderr).strip() or out.strip()
        raise RuntimeError(f"Couldn't reach WSL distro '{distro or 'default'}'.\n\n{err}")
    return [int(m) for m in re.findall(r"MISSING:(\d+)", out)]


def windows_to_wsl_path(p: str) -> str:
    """Convert a path picked in a Windows dialog to the matching Linux path."""
    p = p.replace("\\", "/")
    m = re.match(r"^//wsl(?:\.localhost|\$)/[^/]+(/.*)?$", p, re.IGNORECASE)
    if m:
        return m.group(1) or "/"
    m = re.match(r"^([A-Za-z]):(/.*)?$", p)
    if m:
        return f"/mnt/{m.group(1).lower()}{m.group(2) or ''}"
    return p


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class LauncherApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.proc = None
        self.pid_file = None  # Windows: PID file inside WSL for the running launch
        self.log_queue = queue.Queue()

        root.title("FAIRINO Universe Launcher")
        root.minsize(720, 540)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.robot_var = tk.StringVar(value=self.cfg["robot_model"])
        self.gripper_var = tk.StringVar(value=self.cfg["gripper"])
        self.ip_var = tk.StringVar(value=self.cfg["robot_ip_address"])
        self.ws_var = tk.StringVar(value=self.cfg["workspace"])
        self.distro_var = tk.StringVar(value=self.cfg["wsl_distro"])
        self.rebuild_var = tk.BooleanVar(value=False)
        self.cmd_var = tk.StringVar()
        self.ip_msg_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Idle")

        self._build_ui()

        for var in (self.robot_var, self.gripper_var, self.ws_var,
                    self.rebuild_var, self.distro_var):
            var.trace_add("write", lambda *_: self.update_preview())
        self.ip_var.trace_add("write", lambda *_: self.on_ip_changed())
        self.on_ip_changed()

        if IS_WINDOWS:
            self.refresh_distros()

        self.root.after(100, self.poll)

    # ---------------- layout ----------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(4, weight=1)

        # Robot configuration
        cfg_box = ttk.LabelFrame(main, text="Robot configuration", padding=8)
        cfg_box.grid(row=0, column=0, sticky="ew")
        cfg_box.columnconfigure(1, weight=1)

        ttk.Label(cfg_box, text="Robot model").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(cfg_box, textvariable=self.robot_var, values=ROBOT_MODELS,
                     state="readonly", width=18).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(cfg_box, text="Gripper").grid(row=1, column=0, sticky="w", **pad)
        ttk.Combobox(cfg_box, textvariable=self.gripper_var, values=GRIPPERS,
                     width=18).grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(cfg_box, text="Robot IP address").grid(row=2, column=0, sticky="w", **pad)
        ip_row = ttk.Frame(cfg_box)
        ip_row.grid(row=2, column=1, sticky="w", **pad)
        ttk.Entry(ip_row, textvariable=self.ip_var, width=20).pack(side="left")
        self.ip_msg = ttk.Label(ip_row, textvariable=self.ip_msg_var)
        self.ip_msg.pack(side="left", padx=8)

        ttk.Checkbutton(
            cfg_box, variable=self.rebuild_var,
            text="Rebuild hardware plugin (auto-checked when the IP changes)",
        ).grid(row=3, column=1, sticky="w", **pad)

        # Workspace (+ WSL distro on Windows)
        ws_box = ttk.LabelFrame(main, text="Workspace", padding=8)
        ws_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ws_box.columnconfigure(1, weight=1)
        row = 0
        if IS_WINDOWS:
            ttk.Label(ws_box, text="WSL distro\n(blank = default)").grid(
                row=row, column=0, sticky="w", padx=(0, 8), pady=4)
            self.distro_combo = ttk.Combobox(ws_box, textvariable=self.distro_var, width=24)
            self.distro_combo.grid(row=row, column=1, sticky="w", pady=4)
            ttk.Button(ws_box, text="Refresh", command=self.refresh_distros).grid(row=row, column=2, padx=(8, 0))
            row += 1
        ttk.Label(ws_box, text="Path (Linux)" if IS_WINDOWS else "Path").grid(
            row=row, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(ws_box, textvariable=self.ws_var).grid(row=row, column=1, sticky="ew")
        ttk.Button(ws_box, text="Browse…", command=self.browse_ws).grid(row=row, column=2, padx=(8, 0))

        # Command preview
        prev_box = ttk.LabelFrame(main, text="Command", padding=8)
        prev_box.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        prev_box.columnconfigure(0, weight=1)
        ttk.Entry(prev_box, textvariable=self.cmd_var, state="readonly").grid(row=0, column=0, sticky="ew")

        # Buttons + status
        btns = ttk.Frame(main)
        btns.grid(row=3, column=0, sticky="ew", pady=8)
        ttk.Button(btns, text="Save", command=self.on_save).pack(side="left")
        self.launch_btn = ttk.Button(btns, text="Launch", command=self.on_launch)
        self.launch_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Button(btns, text="Clear log", command=self.clear_log).pack(side="left", padx=6)
        ttk.Label(btns, textvariable=self.status_var).pack(side="right")

        # Log
        log_box = ttk.LabelFrame(main, text="Launch output", padding=4)
        log_box.grid(row=4, column=0, sticky="nsew")
        log_box.rowconfigure(0, weight=1)
        log_box.columnconfigure(0, weight=1)
        mono = ("Consolas", 9) if IS_WINDOWS else ("monospace", 9)
        self.log = tk.Text(log_box, wrap="none", height=14, font=mono, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(log_box, orient="horizontal", command=self.log.xview)
        xsb.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.log.tag_configure("err", foreground="#c62828")
        self.log.tag_configure("warn", foreground="#b26a00")
        self.log.tag_configure("gui", foreground="#1565c0")

    # ---------------- state / preview ----------------
    def current_args(self) -> list:
        return build_launch_args(self.robot_var.get(), self.gripper_var.get(),
                                 self.ip_var.get(), self.rebuild_var.get())

    def update_preview(self):
        cmd = " ".join(["ros2", "launch", PACKAGE, LAUNCH_FILE, *self.current_args()])
        if IS_WINDOWS:
            cmd = f"[WSL {self.distro_var.get().strip() or 'default'}] {cmd}"
        self.cmd_var.set(cmd)

    def on_ip_changed(self):
        err = validate_ip(self.ip_var.get())
        if err:
            self.ip_msg_var.set(f"✗ {err}")
            self.ip_msg.configure(foreground="#c62828")
        else:
            changed = self.ip_var.get().strip() != self.cfg.get("last_built_ip")
            self.ip_msg_var.set("✓ (plugin will be rebuilt)" if changed else "✓")
            self.ip_msg.configure(foreground="#2e7d32")
            self.rebuild_var.set(changed)
        self.update_preview()

    def refresh_distros(self):
        distros = list_wsl_distros()
        self.distro_combo.configure(values=distros)
        current = self.distro_var.get().strip()
        if current and distros and current not in distros:
            self.append(f"[gui] WSL distro '{current}' not found. Installed: {', '.join(distros)}\n", "warn")
        elif not distros:
            self.append("[gui] No WSL distros found (is WSL installed?)\n", "warn")

    def browse_ws(self):
        if IS_WINDOWS:
            distro = self.distro_var.get().strip() or default_wsl_distro()
            init = rf"\\wsl.localhost\{distro}\home" if distro else str(Path.home())
            d = filedialog.askdirectory(initialdir=init)
            if d:
                self.ws_var.set(windows_to_wsl_path(d))
        else:
            d = filedialog.askdirectory(initialdir=os.path.expanduser(self.ws_var.get()) or str(Path.home()))
            if d:
                self.ws_var.set(d)

    def collect_cfg(self) -> dict:
        cfg = dict(self.cfg)
        cfg.update(
            robot_model=self.robot_var.get(),
            gripper=self.gripper_var.get().strip() or "none",
            robot_ip_address=self.ip_var.get().strip(),
            workspace=self.ws_var.get().strip(),
            wsl_distro=self.distro_var.get().strip(),
        )
        return cfg

    def validate_all(self) -> bool:
        err = validate_ip(self.ip_var.get())
        if err:
            messagebox.showerror("Invalid IP", err)
            return False
        ws = self.ws_var.get().strip().rstrip("/") or "/"
        ws_setup = ws + "/install/setup.bash"

        if IS_WINDOWS:
            distro = self.distro_var.get().strip()  # blank -> default distro
            labels = [f"ROS 2 setup: {ROS_SETUP}", f"Workspace setup: {ws_setup}"]
            self.status_var.set("Checking WSL…")
            self.root.update_idletasks()
            try:
                missing = wsl_missing_files(distro, [shlex.quote(ROS_SETUP), sh_path(ws_setup)])
            except RuntimeError as exc:
                self.status_var.set("Idle")
                messagebox.showerror("WSL error", str(exc))
                return False
            self.status_var.set("Idle")
            if missing:
                messagebox.showerror(
                    "Not found in WSL",
                    f"In WSL distro '{distro or 'default'}', can't find:\n\n" + "\n".join(labels[i] for i in missing)
                    + "\n\nCheck the distro name, and build the workspace (colcon build).")
                return False
            return True

        ws_setup_path = Path(ws_setup).expanduser()
        if not ws_setup_path.is_file():
            messagebox.showerror(
                "Workspace not built",
                f"Can't find:\n{ws_setup_path}\n\nBuild the workspace first (colcon build).")
            return False
        if not Path(ROS_SETUP).is_file():
            messagebox.showerror("ROS 2 not found", f"Can't find {ROS_SETUP}")
            return False
        return True

    # ---------------- actions ----------------
    def on_save(self):
        if validate_ip(self.ip_var.get()):
            messagebox.showerror("Invalid IP", validate_ip(self.ip_var.get()))
            return
        self.cfg = self.collect_cfg()
        save_config(self.cfg)
        self.append(f"[gui] Saved settings to {CONFIG_PATH}\n", "gui")

    def on_launch(self):
        if self.proc is not None or not self.validate_all():
            return
        self.cfg = self.collect_cfg()
        rebuild = self.rebuild_var.get()

        popen_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                        errors="replace", bufsize=1)
        if IS_WINDOWS:
            self.pid_file = f"/tmp/fairino_gui_{uuid.uuid4().hex}.pid"
            cmd = build_shell_command(self.cfg["workspace"], self.current_args(), self.pid_file)
            argv = wsl_cmd(self.cfg["wsl_distro"], cmd)
            popen_kw["creationflags"] = NO_WINDOW
        else:
            cmd = build_shell_command(self.cfg["workspace"], self.current_args())
            argv = ["bash", "-c", cmd]
            env = dict(os.environ)
            if getattr(sys, "frozen", False):
                # PyInstaller points LD_LIBRARY_PATH at its bundled libs; restore the
                # system value so ros2 and its nodes load the system libraries.
                orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
                if orig is not None:
                    env["LD_LIBRARY_PATH"] = orig
                else:
                    env.pop("LD_LIBRARY_PATH", None)
                env.pop("PYTHONHOME", None)
            popen_kw.update(env=env, start_new_session=True)

        try:
            self.proc = subprocess.Popen(argv, **popen_kw)
        except OSError as exc:
            messagebox.showerror("Launch failed", str(exc))
            self.proc = None
            return

        if rebuild:
            # Plugin gets rebuilt with this IP during this launch
            self.cfg["last_built_ip"] = self.cfg["robot_ip_address"]
        save_config(self.cfg)

        self.append(f"[gui] {self.cmd_var.get()}\n", "gui")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.launch_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set(f"Running (pid {self.proc.pid})")
        self.on_ip_changed()  # refresh rebuild checkbox now that last_built_ip moved

    def on_stop(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        self.status_var.set("Stopping…")
        self.append("[gui] Sending Ctrl+C to launch…\n", "gui")
        self._signal("INT")
        # escalate if it doesn't exit
        self.root.after(10000, lambda p=self.proc: self._escalate(p, "TERM"))
        self.root.after(15000, lambda p=self.proc: self._escalate(p, "KILL"))

    def _escalate(self, proc, sig: str):
        if proc is self.proc and proc.poll() is None:
            self.append(f"[gui] Launch still running, sending SIG{sig}\n", "gui")
            self._signal(sig)

    def _signal(self, sig: str, wait: bool = False):
        """Send SIG<sig> ('INT' / 'TERM' / 'KILL') to the running launch."""
        if self.proc is None:
            return
        if IS_WINDOWS:
            # Signal the launch inside WSL via its PID file; killing wsl.exe
            # alone would leave the Linux processes running.
            pf = shlex.quote(self.pid_file)
            script = f"pid=$(cat {pf} 2>/dev/null) || exit 0; "
            if sig == "KILL":
                script += "pkill -KILL -P $pid; "
            script += f"kill -{sig} $pid 2>/dev/null; true"
            argv = wsl_cmd(self.cfg["wsl_distro"], script)

            def run():
                try:
                    subprocess.run(argv, capture_output=True, timeout=20, creationflags=NO_WINDOW)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if wait:
                run()
            else:
                threading.Thread(target=run, daemon=True).start()
        else:
            try:
                os.killpg(os.getpgid(self.proc.pid), getattr(signal, f"SIG{sig}"))
            except (ProcessLookupError, PermissionError):
                pass

    def _cleanup_pid_file(self):
        if IS_WINDOWS and self.pid_file:
            argv = wsl_cmd(self.cfg["wsl_distro"], f"rm -f {shlex.quote(self.pid_file)}")
            self.pid_file = None
            threading.Thread(
                target=lambda: subprocess.run(argv, capture_output=True, timeout=20,
                                              creationflags=NO_WINDOW),
                daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout:
            self.log_queue.put(ANSI_RE.sub("", line))
        proc.stdout.close()

    def poll(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                tag = None
                if "[ERROR]" in line or "[FATAL]" in line or "process has died" in line:
                    tag = "err"
                elif "[WARN]" in line:
                    tag = "warn"
                self.append(line, tag)
        except queue.Empty:
            pass

        if self.proc is not None and self.proc.poll() is not None:
            code = self.proc.returncode
            self.append(f"[gui] Launch exited with code {code}\n", "gui")
            self.proc = None
            self._cleanup_pid_file()
            self.launch_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.status_var.set(f"Exited ({code})")

        self.root.after(100, self.poll)

    def append(self, text, tag=None):
        at_bottom = self.log.yview()[1] >= 0.999
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        extra = int(self.log.index("end-1c").split(".")[0]) - MAX_LOG_LINES
        if extra > 0:
            self.log.delete("1.0", f"{extra + 1}.0")
        self.log.configure(state="disabled")
        if at_bottom:
            self.log.see("end")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def on_close(self):
        if self.proc is not None and self.proc.poll() is None:
            if not messagebox.askyesno("Launch running", "Stop the running launch and quit?"):
                return
            self._signal("INT", wait=True)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._signal("KILL", wait=True)
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if IS_WINDOWS else "clam")
    except tk.TclError:
        pass
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
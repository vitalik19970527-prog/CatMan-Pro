import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = WORKSPACE_ROOT / "scripts" / "build_release.ps1"
CONFIG_FILE = WORKSPACE_ROOT / "config.py"
INSTALLER_ISS_FILE = WORKSPACE_ROOT / "scripts" / "CatManPro.installer.iss"
PYTHON_EXE = WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe"


class ReleaseBuilderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CatManPro Release Builder")
        self.root.geometry("860x650")

        self.version_var = tk.StringVar(value=self._detect_version())
        self.app_var = tk.StringVar(value="main")
        self.setup_var = tk.BooleanVar(value=True)
        self.apk_var = tk.BooleanVar(value=False)
        self.skip_protection_var = tk.BooleanVar(value=True)
        self.sync_policy_var = tk.BooleanVar(value=True)
        self.copy_desktop_var = tk.BooleanVar(value=True)

        self._build_ui()

    def _detect_version(self) -> str:
        if not CONFIG_FILE.exists():
            return "1.0.2"
        text = CONFIG_FILE.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
        return match.group(1).strip() if match else "1.0.2"

    def _build_ui(self) -> None:
        top = tk.Frame(self.root, padx=12, pady=10)
        top.pack(fill=tk.X)

        tk.Label(top, text="Version:", font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        tk.Entry(
            top, textvariable=self.version_var, width=24, font=("Segoe UI", 10)
        ).grid(row=0, column=1, padx=8, sticky="w")

        tk.Label(
            top,
            text="Format: MAJOR.MINOR.PATCH (example: 1.0.2)",
            font=("Segoe UI", 9),
            fg="#555",
        ).grid(row=0, column=2, sticky="w")

        app_box = tk.LabelFrame(self.root, text="Which app", padx=12, pady=8)
        app_box.pack(fill=tk.X, padx=12, pady=(0, 8))

        tk.Radiobutton(
            app_box, text="CatMan Pro (main)", variable=self.app_var, value="main"
        ).pack(anchor="w")
        tk.Radiobutton(
            app_box,
            text="CatMan SupAdm (superadmin mini-app)",
            variable=self.app_var,
            value="superadmin",
        ).pack(anchor="w")
        tk.Radiobutton(
            app_box, text="Both", variable=self.app_var, value="both"
        ).pack(anchor="w")

        options = tk.LabelFrame(self.root, text="Build targets", padx=12, pady=8)
        options.pack(fill=tk.X, padx=12, pady=(0, 8))

        tk.Checkbutton(
            options, text="Installer (Setup .exe)", variable=self.setup_var
        ).pack(anchor="w")
        tk.Checkbutton(options, text="Android APK", variable=self.apk_var).pack(
            anchor="w"
        )
        tk.Checkbutton(
            options, text="Copy result(s) to Desktop", variable=self.copy_desktop_var
        ).pack(anchor="w")

        sync_box = tk.LabelFrame(
            self.root, text="Version sync (automatic)", padx=12, pady=8
        )
        sync_box.pack(fill=tk.X, padx=12, pady=(0, 8))

        tk.Label(
            sync_box,
            text="Before build, entered version will be written to config.py and installer defaults.",
            font=("Segoe UI", 9),
            fg="#444",
        ).pack(anchor="w")

        tk.Checkbutton(
            sync_box,
            text="Also update DB app update policy: min_required = entered version",
            variable=self.sync_policy_var,
        ).pack(anchor="w", pady=(6, 0))

        extra = tk.LabelFrame(self.root, text="Extra", padx=12, pady=8)
        extra.pack(fill=tk.X, padx=12, pady=(0, 8))

        tk.Checkbutton(
            extra,
            text="Skip code protection (recommended for local/trial PyArmor)",
            variable=self.skip_protection_var,
        ).pack(anchor="w")

        buttons = tk.Frame(self.root, padx=12, pady=4)
        buttons.pack(fill=tk.X)

        self.run_btn = tk.Button(
            buttons, text="Build selected", width=16, command=self.start_build
        )
        self.run_btn.pack(side=tk.LEFT)
        tk.Button(buttons, text="Clear log", width=12, command=self.clear_log).pack(
            side=tk.LEFT, padx=8
        )

        self.log = scrolledtext.ScrolledText(
            self.root, wrap=tk.WORD, font=("Consolas", 10)
        )
        self.log.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        self.log.insert(tk.END, f"Workspace: {WORKSPACE_ROOT}\n")

    def clear_log(self) -> None:
        self.log.delete("1.0", tk.END)

    def append_log(self, line: str) -> None:
        self.log.insert(tk.END, line)
        self.log.see(tk.END)
        self.root.update_idletasks()

    def _validate_version(self) -> str:
        version = self.version_var.get().strip()
        if not version:
            raise ValueError("Version is required")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("Version must be in format MAJOR.MINOR.PATCH (e.g. 1.0.2)")
        return version

    def _replace_single_regex(
        self, file_path: Path, pattern: str, replacement: str, label: str
    ) -> None:
        if not file_path.exists():
            raise ValueError(f"{label} file not found: {file_path}")

        original = file_path.read_text(encoding="utf-8", errors="ignore")
        updated, count = re.subn(pattern, replacement, original, count=1, flags=re.M)
        if count != 1:
            raise ValueError(
                f"Could not update {label}. Expected one match for pattern: {pattern}"
            )
        file_path.write_text(updated, encoding="utf-8")

    def _sync_version_everywhere(self, version: str) -> None:
        self.append_log(f"Sync version to files: {version}\n")

        self._replace_single_regex(
            CONFIG_FILE,
            r"^\s*APP_VERSION\s*=\s*['\"][^'\"]+['\"]",
            f'    APP_VERSION = "{version}"',
            "config.py APP_VERSION",
        )
        self.append_log("  OK: config.py APP_VERSION updated\n")

        self._replace_single_regex(
            INSTALLER_ISS_FILE,
            r"^\s*#define\s+AppVersion\s+\"[^\"]+\"",
            f'  #define AppVersion "{version}"',
            "installer AppVersion",
        )
        self.append_log("  OK: CatManPro.installer.iss AppVersion updated\n")

        if self.sync_policy_var.get():
            self._sync_update_policy(version)

    def _sync_update_policy(self, version: str) -> None:
        if not PYTHON_EXE.exists():
            raise ValueError(f"Python not found: {PYTHON_EXE}")

        code = (
            "from database import DataManager\n"
            f"version = '{version}'\n"
            "db = DataManager()\n"
            "policy = db.get_app_update_policy() or {}\n"
            "url = str(policy.get('download_url') or '').strip()\n"
            "ok = db.set_app_update_policy({'min_required': version, 'download_url': url})\n"
            "print('OK' if ok else 'FAIL')\n"
            "print('URL=' + url)\n"
        )
        result = subprocess.run(
            [str(PYTHON_EXE), "-c", code],
            cwd=str(WORKSPACE_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()

        if result.returncode != 0:
            raise ValueError(f"Failed to update DB policy: {err or out}")
        if "OK" not in out:
            raise ValueError(f"DB policy update returned unexpected output: {out}")

        self.append_log("  OK: DB app_update_policy min_required updated\n")
        for line in out.splitlines():
            self.append_log(f"    {line}\n")

    def _build_command(self, version: str) -> list[str]:
        if not (self.setup_var.get() or self.apk_var.get()):
            raise ValueError("Select at least one target")

        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BUILD_SCRIPT),
            "-Version",
            version,
            "-App",
            self.app_var.get(),
        ]

        if self.skip_protection_var.get():
            cmd.append("-SkipCodeProtection")
        if not self.setup_var.get():
            cmd.append("-NoSetup")
        if self.apk_var.get():
            cmd.append("-BuildApk")
        if self.copy_desktop_var.get():
            cmd.append("-CopyToDesktop")

        return cmd

    def start_build(self) -> None:
        try:
            version = self._validate_version()
            self._sync_version_everywhere(version)
            cmd = self._build_command(version)
        except ValueError as exc:
            messagebox.showerror("Release Builder", str(exc))
            return

        self.run_btn.config(state=tk.DISABLED)
        self.append_log("\n=== Build started ===\n")
        self.append_log(f"Version: {version}\n")
        self.append_log("Command: " + " ".join(cmd) + "\n\n")

        thread = threading.Thread(target=self._run_build, args=(cmd,), daemon=True)
        thread.start()

    def _run_build(self, cmd: list[str]) -> None:
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(WORKSPACE_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            assert proc.stdout is not None
            for line in proc.stdout:
                self.root.after(0, self.append_log, line)

            code = proc.wait()
            if code == 0:
                self.root.after(
                    0, self.append_log, "\n=== Build finished successfully ===\n"
                )
                self.root.after(
                    0,
                    messagebox.showinfo,
                    "Release Builder",
                    "Build finished successfully",
                )
            else:
                self.root.after(
                    0, self.append_log, f"\n=== Build failed (exit code {code}) ===\n"
                )
                self.root.after(
                    0,
                    messagebox.showerror,
                    "Release Builder",
                    f"Build failed (exit code {code})",
                )
        except Exception as exc:
            self.root.after(0, self.append_log, f"\nBuild error: {exc}\n")
            self.root.after(
                0, messagebox.showerror, "Release Builder", f"Build error: {exc}"
            )
        finally:
            self.root.after(0, lambda: self.run_btn.config(state=tk.NORMAL))


def main() -> None:
    if not BUILD_SCRIPT.exists():
        messagebox.showerror(
            "Release Builder", f"Build script not found: {BUILD_SCRIPT}"
        )
        return

    root = tk.Tk()
    ReleaseBuilderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

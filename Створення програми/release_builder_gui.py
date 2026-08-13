import re
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, scrolledtext

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = WORKSPACE_ROOT / "scripts" / "build_release.ps1"
CALC_BUILD_SCRIPT = WORKSPACE_ROOT / "scripts" / "build_calculator_release.ps1"
INSTALLER_ISS_FILE = WORKSPACE_ROOT / "scripts" / "CatManPro.installer.iss"
PYTHON_EXE = WORKSPACE_ROOT / ".venv" / "Scripts" / "python.exe"

# Each of the 4 apps carries its OWN independent version number and its own
# DB update-policy row (see database.py's get/set_*_update_policy pair for
# each) — Pro/SupAdm/GSMParser build through the shared build_release.ps1
# (BuildScriptProfile selects which $appProfiles entry there), Calculator
# through its own separate build_calculator_release.ps1.
APP_DEFS = [
    {
        "key": "main",
        "label": "CatMan Pro",
        "version_file": WORKSPACE_ROOT / "config.py",
        "build_script_profile": "main",
        "policy_getter": "get_app_update_policy",
        "policy_setter": "set_app_update_policy",
    },
    {
        "key": "superadmin",
        "label": "CatMan SupAdm",
        "version_file": WORKSPACE_ROOT / "superadmin_app.py",
        "build_script_profile": "superadmin",
        "policy_getter": "get_supadm_update_policy",
        "policy_setter": "set_supadm_update_policy",
    },
    {
        "key": "gsmparser",
        "label": "CatMan GSM Parser",
        "version_file": WORKSPACE_ROOT / "gsm_parser_app.py",
        "build_script_profile": "gsmparser",
        "policy_getter": "get_gsm_parser_update_policy",
        "policy_setter": "set_gsm_parser_update_policy",
    },
    {
        "key": "calculator",
        "label": "CatMan Калькулятори",
        "version_file": WORKSPACE_ROOT / "calculator_app.py",
        "build_script_profile": None,  # own script, see CALC_BUILD_SCRIPT
        "policy_getter": "get_calculator_update_policy",
        "policy_setter": "set_calculator_update_policy",
    },
]


class ReleaseBuilderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("CatManPro Release Builder")
        self.root.geometry("900x720")

        self.setup_var = tk.BooleanVar(value=True)
        self.apk_var = tk.BooleanVar(value=False)
        self.skip_protection_var = tk.BooleanVar(value=True)
        self.sync_policy_var = tk.BooleanVar(value=True)
        self.copy_desktop_var = tk.BooleanVar(value=True)

        # Per-app enabled + version vars, in APP_DEFS order.
        self.app_enabled_vars: dict[str, tk.BooleanVar] = {}
        self.app_version_vars: dict[str, tk.StringVar] = {}
        for app in APP_DEFS:
            self.app_enabled_vars[app["key"]] = tk.BooleanVar(
                value=(app["key"] == "main")
            )
            self.app_version_vars[app["key"]] = tk.StringVar(
                value=self._detect_version(app["version_file"])
            )

        self._build_ui()

    def _detect_version(self, version_file: Path) -> str:
        if not version_file.exists():
            return "1.0.0"
        text = version_file.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
        return match.group(1).strip() if match else "1.0.0"

    def _build_ui(self) -> None:
        app_box = tk.LabelFrame(self.root, text="Apps to build", padx=12, pady=8)
        app_box.pack(fill=tk.X, padx=12, pady=(10, 8))

        tk.Label(
            app_box,
            text="Check any combination — one button press builds all checked apps"
            " in sequence, each with its own version.",
            font=("Segoe UI", 9),
            fg="#555",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        for i, app in enumerate(APP_DEFS, start=1):
            key = app["key"]
            tk.Checkbutton(
                app_box, text=app["label"], variable=self.app_enabled_vars[key],
                width=22, anchor="w",
            ).grid(row=i, column=0, sticky="w")
            tk.Label(app_box, text="Version:").grid(row=i, column=1, sticky="e", padx=(8, 4))
            tk.Entry(
                app_box, textvariable=self.app_version_vars[key], width=16,
            ).grid(row=i, column=2, sticky="w")

        options = tk.LabelFrame(self.root, text="Build targets", padx=12, pady=8)
        options.pack(fill=tk.X, padx=12, pady=(0, 8))

        tk.Checkbutton(
            options, text="Installer (Setup .exe) — applies to Pro/SupAdm/GSM Parser;"
            " Calculator's own script always builds its installer",
            variable=self.setup_var,
        ).pack(anchor="w")
        tk.Checkbutton(
            options, text="Android APK (Pro/SupAdm/GSM Parser only)", variable=self.apk_var
        ).pack(anchor="w")
        tk.Checkbutton(
            options, text="Copy result(s) to Desktop", variable=self.copy_desktop_var
        ).pack(anchor="w")

        sync_box = tk.LabelFrame(
            self.root, text="Version sync (automatic)", padx=12, pady=8
        )
        sync_box.pack(fill=tk.X, padx=12, pady=(0, 8))

        tk.Label(
            sync_box,
            text="Before build, each checked app's own version is written to its own"
            " file (config.py for Pro; its own APP_VERSION constant for the rest).",
            font=("Segoe UI", 9),
            fg="#444",
        ).pack(anchor="w")

        tk.Checkbutton(
            sync_box,
            text="Also update each app's own DB update policy: min_required = entered version",
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

    def _selected_apps(self) -> list[dict]:
        return [app for app in APP_DEFS if self.app_enabled_vars[app["key"]].get()]

    def _validate(self, selected: list[dict]) -> dict[str, str]:
        if not selected:
            raise ValueError("Select at least one app to build")
        versions: dict[str, str] = {}
        for app in selected:
            version = self.app_version_vars[app["key"]].get().strip()
            if not re.fullmatch(r"\d+\.\d+\.\d+", version):
                raise ValueError(
                    f"{app['label']}: version must be MAJOR.MINOR.PATCH (e.g. 1.0.2)"
                )
            versions[app["key"]] = version
        if not self.setup_var.get() and not self.apk_var.get():
            raise ValueError("Select at least one build target: Installer or APK")
        return versions

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

    def _sync_app_version(self, app: dict, version: str) -> None:
        self._replace_single_regex(
            app["version_file"],
            r"APP_VERSION\s*=\s*[\"'][^\"']+[\"']",
            f'APP_VERSION = "{version}"',
            f"{app['label']} version file",
        )
        self.append_log(f"  OK: {app['version_file'].name} APP_VERSION -> {version}\n")

        # Pro/SupAdm/GSM Parser all share CatManPro.installer.iss (see
        # build_release.ps1's $appProfiles — it's parameterized per profile
        # via /D overrides at compile time, not per-file). Its #define
        # AppVersion is only a fallback default (build_release.ps1 always
        # passes /DAppVersion explicitly), so this sync is cosmetic — kept
        # in step with Pro's own version specifically, since that's the
        # profile this installer script was originally authored for.
        if app["key"] == "main":
            self._replace_single_regex(
                INSTALLER_ISS_FILE,
                r"#define\s+AppVersion\s+\"[^\"]+\"",
                f'  #define AppVersion "{version}"',
                "installer AppVersion",
            )
            self.append_log("  OK: CatManPro.installer.iss AppVersion updated\n")

    def _sync_update_policy(self, app: dict, version: str) -> None:
        if not PYTHON_EXE.exists():
            raise ValueError(f"Python not found: {PYTHON_EXE}")

        code = (
            "from database import DataManager\n"
            f"version = '{version}'\n"
            "db = DataManager()\n"
            f"policy = db.{app['policy_getter']}() or {{}}\n"
            "url = str(policy.get('download_url') or '').strip()\n"
            "payload = {'min_required': version, 'download_url': url}\n"
            "extra_url = str(policy.get('download_url_web') or '').strip()\n"
            "if extra_url:\n"
            "    payload['download_url_web'] = extra_url\n"
            "extra_android = str(policy.get('download_url_android') or '').strip()\n"
            "if extra_android:\n"
            "    payload['download_url_android'] = extra_android\n"
            f"ok = db.{app['policy_setter']}(payload)\n"
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
            raise ValueError(f"Failed to update {app['label']} DB policy: {err or out}")
        if "OK" not in out:
            raise ValueError(f"{app['label']} DB policy update returned unexpected output: {out}")

        self.append_log(f"  OK: {app['label']} DB update policy min_required updated\n")
        for line in out.splitlines():
            self.append_log(f"    {line}\n")

    def _build_commands(self, selected: list[dict], versions: dict[str, str]) -> list[tuple[str, list[str]]]:
        """One (label, cmd) pair per selected app — run sequentially so
        PyInstaller/Inno Setup/Gradle never contend for the same build
        directories or resources at once."""
        commands: list[tuple[str, list[str]]] = []
        for app in selected:
            version = versions[app["key"]]
            if app["build_script_profile"] is not None:
                cmd = [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(BUILD_SCRIPT),
                    "-Version", version,
                    "-App", app["build_script_profile"],
                ]
                if self.skip_protection_var.get():
                    cmd.append("-SkipCodeProtection")
                if not self.setup_var.get():
                    cmd.append("-NoSetup")
                if self.apk_var.get():
                    cmd.append("-BuildApk")
                if self.copy_desktop_var.get():
                    cmd.append("-CopyToDesktop")
            else:
                # Calculator — own script, own param surface (no APK/NoSetup).
                cmd = [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(CALC_BUILD_SCRIPT),
                    "-Version", version,
                ]
                if self.copy_desktop_var.get():
                    cmd.append("-CopyToDesktop")
            commands.append((app["label"], cmd))
        return commands

    def start_build(self) -> None:
        try:
            selected = self._selected_apps()
            versions = self._validate(selected)
            self.append_log("\n=== Syncing versions ===\n")
            for app in selected:
                version = versions[app["key"]]
                self.append_log(f"{app['label']}: {version}\n")
                self._sync_app_version(app, version)
                if self.sync_policy_var.get():
                    self._sync_update_policy(app, version)
            commands = self._build_commands(selected, versions)
        except ValueError as exc:
            messagebox.showerror("Release Builder", str(exc))
            return

        self.run_btn.config(state=tk.DISABLED)
        self.append_log("\n=== Build started ===\n")
        for label, cmd in commands:
            self.append_log(f"[{label}] Command: " + " ".join(cmd) + "\n")

        thread = threading.Thread(target=self._run_builds, args=(commands,), daemon=True)
        thread.start()

    def _run_builds(self, commands: list[tuple[str, list[str]]]) -> None:
        try:
            for label, cmd in commands:
                self.root.after(0, self.append_log, f"\n--- Building: {label} ---\n")
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
                if code != 0:
                    self.root.after(
                        0, self.append_log,
                        f"\n=== Build FAILED for {label} (exit code {code}) — stopping ===\n",
                    )
                    self.root.after(
                        0, messagebox.showerror, "Release Builder",
                        f"Build failed for {label} (exit code {code})",
                    )
                    return

            self.root.after(0, self.append_log, "\n=== All builds finished successfully ===\n")
            self.root.after(
                0, messagebox.showinfo, "Release Builder", "All builds finished successfully"
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
    if not CALC_BUILD_SCRIPT.exists():
        messagebox.showerror(
            "Release Builder", f"Calculator build script not found: {CALC_BUILD_SCRIPT}"
        )
        return

    root = tk.Tk()
    ReleaseBuilderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

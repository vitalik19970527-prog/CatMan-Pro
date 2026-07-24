"""Strip developer-only personal data out of the release payload before
packaging (PyInstaller exe / flet build apk). Run from the repo root.

Resets the local quick-login/account files to their empty defaults and
blanks personal identity fields inside .env — everything an end user's
installed app is expected to (re)create for itself at runtime, or that
just identifies the developer's own machine. Deployment settings
(DATABASE_URL, cache/sync tuning, etc.) are left untouched so the built
app still connects and works out of the box.

Called by scripts/build_release.ps1 (local builds) and by the
release-catman.yml / build-mobile.yml GitHub Actions workflows (CI
builds) as a "Sanitize local data files" step before the actual build.
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

QUICK_ACCOUNTS = os.path.join(ROOT, "quick_accounts.json")
SUPER_ADMIN_ACCOUNT = os.path.join(ROOT, "super_admin_account.json")
CONFIG_TXT = os.path.join(ROOT, "config.txt")
ENV_FILE = os.path.join(ROOT, ".env")

# .env keys that identify a specific person or dev machine rather than
# configure the app's deployment — never shipped in a release build.
ENV_PERSONAL_KEYS = {"SUPERADMIN_EMAIL", "WORK_EMAIL", "GSM_PARSER_GOOGLE_JSON"}


def _write_json(path, data):
    if not os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"sanitized {path}")


def sanitize_quick_accounts():
    _write_json(QUICK_ACCOUNTS, {"accounts": []})


def sanitize_super_admin_account():
    _write_json(
        SUPER_ADMIN_ACCOUNT,
        {"company": "", "manager": "", "email": "", "phone": ""},
    )


def sanitize_config_txt():
    if not os.path.exists(CONFIG_TXT):
        return
    with open(CONFIG_TXT, "w", encoding="utf-8") as f:
        f.write("")
    print(f"sanitized {CONFIG_TXT}")


def sanitize_env():
    if not os.path.exists(ENV_FILE):
        return
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in ENV_PERSONAL_KEYS:
                out.append(f"{key}=\n")
                continue
        out.append(line)

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(out)
    print(f"sanitized {ENV_FILE} (cleared: {', '.join(sorted(ENV_PERSONAL_KEYS))})")


def main():
    sanitize_quick_accounts()
    sanitize_super_admin_account()
    sanitize_config_txt()
    sanitize_env()


if __name__ == "__main__":
    main()

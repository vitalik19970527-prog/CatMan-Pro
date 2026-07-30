"""
Flet's default Android build template (cached by cookiecutter at
~/.cookiecutters/flet-build-template.zip) only declares a <queries> entry for
ACTION_PROCESS_TEXT in AndroidManifest.xml. Starting with Android 11 (API 30),
apps must explicitly declare package-visibility <queries> for any other
implicit intent they want to resolve/launch — otherwise `canLaunchUrl`/
`launch_url` (url_launcher plugin) silently no-ops: no exception, no browser
opening, nothing. This is exactly the bug where tapping "Скачати оновлення"
on Android does nothing at all.

This script patches the cached template zip in place, adding a <queries>
entry for ACTION_VIEW + http/https so `page.launch_url()` can actually open
the GitHub release asset URL (browser download prompt) on Android 11+.

Idempotent: safe to run every time before `flet build apk`; skips if already
patched, and no-ops (with a clear message) if the template zip hasn't been
downloaded yet (it will be recreated as the un-patched original by flet on
the next build — re-run this script afterward to patch it).
"""
import io
import os
import zipfile

TEMPLATE_ZIP = os.path.join(
    os.path.expanduser("~"), ".cookiecutters", "flet-build-template.zip"
)
MANIFEST_ENTRY = "build/{{cookiecutter.out_dir}}/android/app/src/main/AndroidManifest.xml"

OLD_QUERIES = """    <queries>
        <intent>
            <action android:name="android.intent.action.PROCESS_TEXT" />
            <data android:mimeType="text/plain" />
        </intent>
    </queries>"""

NEW_QUERIES = """    <queries>
        <intent>
            <action android:name="android.intent.action.PROCESS_TEXT" />
            <data android:mimeType="text/plain" />
        </intent>
        <!-- Lets page.launch_url() actually open http(s) URLs on Android 11+
             (package visibility) — without this, launch_url()/canLaunchUrl()
             silently no-op instead of opening the browser. -->
        <intent>
            <action android:name="android.intent.action.VIEW" />
            <data android:scheme="https" />
        </intent>
        <intent>
            <action android:name="android.intent.action.VIEW" />
            <data android:scheme="http" />
        </intent>
    </queries>"""


def main() -> None:
    if not os.path.isfile(TEMPLATE_ZIP):
        print(
            f"[patch_flet_android_template] {TEMPLATE_ZIP} not found yet — "
            "run `flet build apk` once first to let flet download it, then "
            "re-run this script before the next build."
        )
        return

    with zipfile.ZipFile(TEMPLATE_ZIP, "r") as zin:
        if MANIFEST_ENTRY not in zin.namelist():
            print(
                f"[patch_flet_android_template] {MANIFEST_ENTRY} not found in "
                "the template zip — flet's template layout may have changed; "
                "skipping patch."
            )
            return
        manifest_src = zin.read(MANIFEST_ENTRY).decode("utf-8")

        if "package visibility" in manifest_src:
            print("[patch_flet_android_template] Already patched — nothing to do.")
            return

        if OLD_QUERIES not in manifest_src:
            print(
                "[patch_flet_android_template] Expected <queries> block not "
                "found verbatim — flet's template changed; skipping patch "
                "(url_launcher on Android 11+ may silently fail until this "
                "is re-applied by hand)."
            )
            return

        patched_manifest = manifest_src.replace(OLD_QUERIES, NEW_QUERIES)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = (
                    patched_manifest.encode("utf-8")
                    if item.filename == MANIFEST_ENTRY
                    else zin.read(item.filename)
                )
                zout.writestr(item, data)

    with open(TEMPLATE_ZIP, "wb") as f:
        f.write(buf.getvalue())

    print(
        "[patch_flet_android_template] Patched AndroidManifest.xml in "
        f"{TEMPLATE_ZIP} — added VIEW/http/https to <queries>."
    )


if __name__ == "__main__":
    main()

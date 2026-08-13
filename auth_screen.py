import asyncio
import json
import os
import shutil
import sys

import flet as ft

import i18n
from config import Config
from database import DataManager
from logger import logger


class AuthScreen:
    def __init__(
        self,
        page: ft.Page,
        on_success,
        *,
        allow_registration: bool = True,
        on_settings_click=None,
    ):
        self.page = page
        self.db = DataManager()
        i18n.init_i18n(self.db)
        self.t = i18n.bind("")  # pre-login: always session-level language
        # Standalone single-purpose apps (CatManGSMParser.exe) reuse this
        # exact same login screen/saved-account logic as CatMan Pro, but
        # must not let visitors self-register a brand new account through
        # them — only main.py's own AuthScreen() call leaves this True.
        self.allow_registration = bool(allow_registration)
        # Opt-in gear icon on the login card — lets a standalone app expose
        # app_storage's cache-relocation setting (move from C: to D:, same
        # as views/settings_view.py's Settings tab) without needing a full
        # dashboard around it. None (the default, main.py's own usage)
        # means no gear icon at all.
        self.on_settings_click = on_settings_click
        # Translation seeding used to run here, synchronously, before any UI
        # painted — over localhost that was fast enough to be invisible, but
        # over a remote VPS connection it added several real round trips
        # (each call inserts/checks hundreds of rows) BEFORE the login form
        # could even appear, which is exactly the "long black window" the
        # user is stuck looking at. t()/bind() don't need seeding to have
        # finished — they always have the UA literal as an immediate
        # fallback — so this is now kicked off in the background from
        # show_login() instead, after the form is already on screen.
        self._i18n_seed_started = False
        self.on_success = on_success
        self.roles = [
            "Закупівельник",
            "Категорійний менеджер",
            "Керівник направлення",
            "Керівник",
            "Адмін",
            "СуперАдмін",
        ]
        # 5 основних валют системи
        self.currencies_list = ["USD", "UAH", "EUR", "PLN", "CNY"]
        data_dir = self._app_data_dir()
        self.config_file = os.path.join(data_dir, "config.txt")
        self.quick_accounts_file = os.path.join(data_dir, "quick_accounts.json")
        self.super_admin_account_file = os.path.join(
            data_dir, "super_admin_account.json"
        )
        self.super_admin_email = (
            (getattr(Config, "SUPERADMIN_EMAIL", "") or "").strip().lower()
        )
        self.work_email = (getattr(Config, "WORK_EMAIL", "") or "").strip().lower()
        self._migrate_legacy_local_files(data_dir)

    def _on_session_language_select(self, code: str) -> None:
        """Pre-login language switcher — no manager_id exists yet, so this
        only sets the session-level language (auth_screen.py text itself
        gets translated in a later block)."""
        i18n.set_session_language(code)
        active = getattr(self.page, "_active_auth_screen", None)
        if active == "register":
            self.show_register()
        else:
            self.show_login()

    def _app_data_dir(self):
        target = os.getenv("FLET_APP_STORAGE_DATA") or os.path.join(
            os.getenv("APPDATA") or os.path.expanduser("~"), "CatManPro"
        )
        os.makedirs(target, exist_ok=True)
        return target

    def _migrate_legacy_local_files(self, data_dir):
        """Moves legacy root files to AppData on first run after update."""
        legacy_to_new = {
            "config.txt": os.path.join(data_dir, "config.txt"),
            "quick_accounts.json": os.path.join(data_dir, "quick_accounts.json"),
            "super_admin_account.json": os.path.join(
                data_dir, "super_admin_account.json"
            ),
        }

        # Never migrate from arbitrary working directory in installed builds.
        # This avoids pulling developer/test accounts when launcher CWD points to source repo.
        candidate_dirs = []
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(os.path.abspath(sys.executable or ""))
            if exe_dir:
                candidate_dirs.append(exe_dir)
        else:
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if script_dir:
                candidate_dirs.append(script_dir)
            candidate_dirs.append(os.getcwd())

        seen = set()
        unique_dirs = []
        for folder in candidate_dirs:
            normalized = os.path.normcase(os.path.normpath(folder or ""))
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_dirs.append(folder)

        for legacy_name, target_path in legacy_to_new.items():
            if os.path.exists(target_path):
                continue
            legacy_path = ""
            for folder in unique_dirs:
                probe = os.path.join(folder, legacy_name)
                if os.path.exists(probe):
                    legacy_path = probe
                    break
            if not legacy_path:
                continue
            try:
                shutil.copy2(legacy_path, target_path)
            except Exception:
                logger.exception("Помилка міграції локального файлу %s", legacy_name)

    def _has_local_super_admin_file(self):
        # Локальний super_admin_account.json є головною ознакою "нашого" пристрою.
        # Навіть якщо файл тимчасово містить порожні поля, але quick_accounts існує,
        # все одно вмикаємо селектор для швидкого вибору акаунта.
        if not os.path.exists(self.super_admin_account_file):
            return False

        try:
            with open(self.super_admin_account_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and any(
                (payload.get(k) or "").strip()
                for k in ["company", "manager", "email", "phone"]
            ):
                return True
        except Exception:
            logger.exception("Помилка читання super_admin_account")

        return bool(self._read_quick_accounts())

    def _save_super_admin_account_file(self, company, manager, email, phone):
        try:
            payload = {
                "company": company or "",
                "manager": manager or "",
                "email": email or "",
                "phone": phone or "",
            }
            with open(self.super_admin_account_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Помилка запису super_admin_account")

    def _get_saved_data(self):
        """Читає компанію, ім'я, пошту та телефон з файлу config.txt"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    company = lines[0].strip() if len(lines) >= 1 else ""
                    manager = lines[1].strip() if len(lines) >= 2 else ""
                    email = lines[2].strip() if len(lines) >= 3 else ""
                    phone = lines[3].strip() if len(lines) >= 4 else ""
                    return company, manager, email, phone
            except Exception:
                logger.exception("Помилка читання файлу")
        return "", "", "", ""

    def _save_data_to_file(self, company, manager, email, phone):
        """Записує компанію, ім'я, пошту та телефон у файл config.txt"""
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                f.write(f"{company}\n{manager}\n{email}\n{phone}")
        except Exception:
            logger.exception("Помилка запису у файл")

    def _read_quick_accounts(self):
        payload = self._read_quick_accounts_payload()
        accounts = payload.get("accounts", []) if isinstance(payload, dict) else []
        return accounts if isinstance(accounts, list) else []

    def _read_quick_accounts_payload(self) -> dict:
        if not os.path.exists(self.quick_accounts_file):
            return {}
        try:
            with open(self.quick_accounts_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            return payload if isinstance(payload, dict) else {}
        except Exception:
            logger.exception("Помилка читання quick_accounts")
            return {}

    def _save_quick_accounts(self, accounts, superadmin_enabled: bool | None = None):
        try:
            prev = self._read_quick_accounts_payload()
            if superadmin_enabled is None:
                superadmin_enabled = (
                    bool(prev.get("superadmin_enabled"))
                    if isinstance(prev, dict)
                    else False
                )
            payload = {
                "accounts": accounts,
                "superadmin_enabled": bool(superadmin_enabled),
            }
            with open(self.quick_accounts_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Помилка запису quick_accounts")

    def _normalize_role_key(self, role: str) -> str:
        return (role or "").strip().lower().replace(" ", "")

    def _is_superadmin_account(
        self, email: str | None = None, role: str | None = None
    ) -> bool:
        email_norm = (email or "").strip().lower()
        if (
            self.super_admin_email
            and email_norm
            and email_norm == self.super_admin_email
        ):
            return True
        return self._normalize_role_key(role or "") in {
            "суперадмін",
            "суперадмин",
            "superadmin",
        }

    def _upsert_quick_account(self, company, manager, email, phone, role=None):
        """
        Stores recent logins in quick_accounts.json.
        Limit policy:
        - Keep up to 10 accounts for everyone.
        """
        email_norm = (email or "").strip().lower()
        if not email_norm:
            return

        accounts = self._read_quick_accounts()
        accounts = [a for a in accounts if isinstance(a, dict)]
        prev_payload = self._read_quick_accounts_payload()
        superadmin_enabled = (
            bool(prev_payload.get("superadmin_enabled"))
            if isinstance(prev_payload, dict)
            else False
        )

        phone_digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        company_norm = (company or "").strip().lower()
        manager_norm = (manager or "").strip().lower()

        def _same_user(a: dict) -> bool:
            # Match by email (primary key)
            if (a.get("email") or "").strip().lower() == email_norm:
                return True
            # Match by phone + manager + company (handles email/phone change scenario)
            if phone_digits:
                a_ph = "".join(ch for ch in str(a.get("phone") or "") if ch.isdigit())
                a_mgr = (a.get("manager") or "").strip().lower()
                a_co = (a.get("company") or "").strip().lower()
                if a_ph == phone_digits and a_mgr == manager_norm and a_co == company_norm:
                    return True
            return False

        # Remove all matching entries (LRU move-to-front)
        accounts = [a for a in accounts if not _same_user(a)]

        accounts.insert(
            0,
            {
                "company": company or "",
                "manager": manager or "",
                "email": email or "",
                "phone": phone or "",
                "role": (role or "").strip(),
                "last_used_ts": int(__import__("time").time()),
            },
        )

        limit = 10
        self._save_quick_accounts(
            accounts[: max(1, int(limit))], superadmin_enabled=superadmin_enabled
        )

    def _sync_quick_account_roles_from_db(self, db=None) -> None:
        """Підтягує текст ролі з Postgres у quick_accounts.json (email + назва компанії в збереженому рядку).

        Accepts an optional `db` override so the background caller (see
        `_start_quick_role_sync_background`) can pass its own short-lived
        DataManager instead of sharing `self.db` — that connection may be
        used concurrently by an interactive login submit on another thread,
        and a single psycopg connection isn't safe for concurrent queries
        from two threads at once."""
        dbc = db if db is not None else self.db
        prev = self._read_quick_accounts_payload()
        if not isinstance(prev, dict):
            prev = {}
        raw = prev.get("accounts")
        accounts_list = raw if isinstance(raw, list) else []
        if not accounts_list:
            return

        new_accounts: list = []
        changed = False
        for item in accounts_list:
            if not isinstance(item, dict):
                new_accounts.append(item)
                continue
            entry = dict(item)
            ek = (entry.get("email") or "").strip().lower()
            comp = (entry.get("company") or "").strip()
            if not ek:
                new_accounts.append(entry)
                continue
            label = ""
            try:
                if dbc._use_postgres():
                    label = (
                        dbc.resolve_quick_account_role_label(ek, comp) or ""
                    ).strip()
            except Exception:
                logger.exception("Не вдалося підтягнути роль для quick account")
            cur = str(entry.get("role") or "").strip()
            if label and label != cur:
                entry["role"] = label
                changed = True

            # Sync company name if missing (non-SA accounts)
            if not comp:
                try:
                    db_comp = dbc.get_company_name_for_manager_email(ek)
                    if db_comp:
                        entry["company"] = db_comp
                        # Re-resolve role with correct company for accuracy
                        fresh_label = (
                            dbc.resolve_quick_account_role_label(ek, db_comp) or label
                        ).strip()
                        if fresh_label:
                            entry["role"] = fresh_label
                        changed = True
                except Exception:
                    pass

            new_accounts.append(entry)

        if not changed:
            return
        prev_sa = (
            bool(prev.get("superadmin_enabled")) if isinstance(prev, dict) else False
        )
        self._save_quick_accounts(new_accounts[:10], superadmin_enabled=prev_sa)

    def show_login(self, e=None):
        """Вікно авторизації з підтримкою автозаповнення та клавіші Enter"""
        self.page.clean()
        self.page._active_auth_screen = "login"

        saved_company, saved_manager, saved_email, saved_phone = self._get_saved_data()
        quick_accounts = self._read_quick_accounts()

        # Елемент для виводу загальної помилки авторизації
        error_message = ft.Text(
            value="", color="red", size=12, weight="bold", text_align="center"
        )

        def handle_login_click(e):
            # Очищення попередніх помилок
            error_message.value = ""
            comp_name.error_text = None
            manager_name.error_text = None
            email_field.error_text = None
            phone_field.error_text = None
            password.error_text = None

            # Перевірка кожного поля окремо
            error = False
            if not (manager_name.value or "").strip():
                manager_name.error_text = self.t("auth.err.missing_name", "Не введено: Ім'я")
                error = True
            if not (email_field.value or "").strip():
                email_field.error_text = self.t("auth.err.missing_email", "Не введено: E-mail")
                error = True
            if not (phone_field.value or "").strip():
                phone_field.error_text = self.t("auth.err.missing_phone", "Не введено: Телефон")
                error = True
            if not password.value:
                password.error_text = self.t("auth.err.missing_password", "Не введено: Пароль")
                error = True
            # Not gated client-side: whether an empty company is valid (СуперАдмін) is decided server-side by check_auth.

            if error:
                self._show_snack(
                    self.t("auth.login.err_check_fields", "Перевірте незаповнені поля"),
                    "orange",
                )
                self.page.update()
                return

            try:
                user = self.db.check_auth(
                    comp_name.value,
                    manager_name.value,
                    password.value,
                    email_field.value,
                    phone_field.value,
                )
            except Exception:
                logger.exception("Authentication check failed")
                error_message.value = self.t(
                    "auth.login.err_auth_failed", "Помилка авторизації. Спробуйте пізніше."
                )
                self.page.update()
                return

            if user:
                if user.get("__company_blocked__"):
                    blocked_msg = self.t(
                        "auth.login.err_company_blocked",
                        "Компанію заблоковано. Зверніться до адміністратора.",
                    )
                    error_message.value = blocked_msg
                    error_message.color = "red"
                    self._show_snack(blocked_msg, "red")
                    self.page.update()
                    return
                if user.get("__company_pending__"):
                    pending_msg = self.t(
                        "auth.login.err_company_pending",
                        "Компанію ще не активовано — суперадмін повинен вказати "
                        "адресу сервера. Зверніться до адміністратора.",
                    )
                    error_message.value = pending_msg
                    error_message.color = "red"
                    self._show_snack(pending_msg, "red")
                    self.page.update()
                    return
                if user.get("__blocked__"):
                    blocked_msg = self.t(
                        "auth.login.err_blocked",
                        "Обліковий запис заблоковано. Зверніться до адміністратора.",
                    )
                    error_message.value = blocked_msg
                    error_message.color = "red"
                    self._show_snack(blocked_msg, "red")
                    self.page.update()
                    return
                if user.get("__backup_in_progress__"):
                    backup_msg = self.t(
                        "auth.login.err_backup_in_progress",
                        "Триває технічне обслуговування (резервне копіювання). "
                        "Спробуйте увійти за кілька хвилин.",
                    )
                    error_message.value = backup_msg
                    error_message.color = "red"
                    self._show_snack(backup_msg, "red")
                    self.page.update()
                    return
                elif user.get("is_approved"):
                    # Use canonical company name from DB; SA has no company.
                    _is_sa = self._is_superadmin_account(
                        email=email_field.value, role=user.get("role")
                    )
                    actual_company = (
                        "" if _is_sa
                        else (str(user.get("company_name") or "").strip() or comp_name.value)
                    )
                    self._save_data_to_file(
                        actual_company,
                        manager_name.value,
                        email_field.value,
                        phone_field.value,
                    )
                    self._upsert_quick_account(
                        actual_company,
                        manager_name.value,
                        email_field.value,
                        phone_field.value,
                        role=(user.get("role") or ""),
                    )
                    if (
                        self.super_admin_email
                        and (email_field.value or "").strip().lower()
                        == self.super_admin_email
                    ):
                        self._save_super_admin_account_file(
                            actual_company,
                            manager_name.value,
                            email_field.value,
                            phone_field.value,
                        )
                    self.on_success(user)
                else:
                    not_approved_msg = self.t(
                        "auth.login.err_not_approved",
                        "Доступ ще не підтверджено адміністратором!",
                    )
                    error_message.value = not_approved_msg
                    error_message.color = "orange"
                    self._show_snack(not_approved_msg, "orange")
                    self.page.update()
                    return
            else:
                # ВИВІД НАПИСУ ПРО НЕВІРНІ ДАНІ
                error_message.value = self.t(
                    "auth.login.err_invalid_credentials",
                    "Введені невірні дані, перевірте їх і спробуйте ще раз!",
                )
                error_message.color = "red"
                self.page.update()

        comp_name = ft.TextField(
            label=self.t("auth.common.company_label", "Назва компанії"),
            hint_text=self.t(
                "auth.login.company_hint",
                "Порожньо лише для СуперАдміна — інші користувачі мають вказати компанію",
            ),
            value="",
            border_radius=10,
            prefix_icon="business",
            on_submit=handle_login_click,
        )
        manager_name = ft.TextField(
            label=self.t("auth.common.name_label", "Ваше Ім'я"),
            value="",
            border_radius=10,
            prefix_icon="person",
            on_submit=handle_login_click,
        )
        email_field = ft.TextField(
            label=self.t("auth.common.email_label", "E-mail"),
            value="",
            border_radius=10,
            prefix_icon="email",
            on_submit=handle_login_click,
        )
        phone_field = ft.TextField(
            label=self.t("auth.common.phone_label", "Номер телефону +380"),
            value="",
            border_radius=10,
            prefix_icon="phone",
            on_submit=handle_login_click,
        )
        NEW_ACCOUNT_KEY = "__new__"

        def _quick_slot_key(acc: dict) -> str:
            em = (acc.get("email") or "").strip().lower()
            co = (acc.get("company") or "").strip().lower()
            return f"{em}|{co}"

        password = ft.TextField(
            label=self.t("auth.common.password_label", "Пароль"),
            password=True,
            can_reveal_password=True,
            border_radius=10,
            prefix_icon="lock",
            autofocus=False,
            on_submit=handle_login_click,
        )

        account_options: list = [
            ft.dropdown.Option(
                key=NEW_ACCOUNT_KEY,
                text=self.t(
                    "auth.login.new_account_option",
                    "— Інший акаунт (не збережений, ввести дані вручну) —",
                ),
            )
        ]

        usable = [a for a in quick_accounts if isinstance(a, dict)]
        usable.sort(key=lambda z: int(z.get("last_used_ts") or 0), reverse=True)

        def _acct_dedupe_key(acc: dict) -> tuple[str, str, str]:
            ph = "".join(ch for ch in str(acc.get("phone") or "") if ch.isdigit())
            return (
                ph,
                (acc.get("manager") or "").strip().lower(),
                (acc.get("company") or "").strip().lower(),
            )

        accounts_ordered: list[dict] = []
        dedup_seen: set[tuple[str, str, str]] = set()
        slot_seen: set[str] = set()
        for acc in usable:
            email_key = (acc.get("email") or "").strip().lower()
            if not email_key:
                continue
            dk_acct = _acct_dedupe_key(acc)
            if dk_acct in dedup_seen:
                continue
            dedup_seen.add(dk_acct)
            slot_k = _quick_slot_key(acc)
            if slot_k in slot_seen:
                continue
            slot_seen.add(slot_k)
            accounts_ordered.append(acc)
            manager_name_label = (
                acc.get("manager") or self.t("auth.login.default_user", "Користувач")
            ).strip()
            role_label = (
                acc.get("role") or self.t("auth.login.no_role", "Без посади")
            ).strip()
            comp_hint = (
                (acc.get("company") or "").strip()
                or self.t("auth.login.no_company", "— без компанії —")
            )[:26]
            em_short = email_key[:26] + ("…" if len(email_key) > 26 else "")
            i = len(accounts_ordered) - 1
            account_options.append(
                ft.dropdown.Option(
                    key=str(i),
                    text=f"{manager_name_label} — {role_label} · {comp_hint} ({em_short})",
                )
            )

        accounts_by_dropdown_id: dict[str, dict] = {
            str(i): a for i, a in enumerate(accounts_ordered)
        }
        has_saved_quick = len(accounts_ordered) > 0
        saved_slot = _quick_slot_key(
            {"email": saved_email or "", "company": saved_company or ""}
        )
        default_key = NEW_ACCOUNT_KEY
        if accounts_ordered:
            match_i = None
            for i, acc in enumerate(accounts_ordered):
                if _quick_slot_key(acc) == saved_slot:
                    match_i = str(i)
                    break
            default_key = match_i if match_i is not None else "0"

        def apply_quick_slot_selection(slot_key: str) -> None:
            sk = (slot_key or "").strip()
            error_message.value = ""
            if sk == NEW_ACCOUNT_KEY or not sk:
                comp_name.value = ""
                manager_name.value = ""
                email_field.value = ""
                phone_field.value = ""
                password.value = ""
                self.page.update()
                return
            account = accounts_by_dropdown_id.get(sk)
            if account is None and sk.isdigit():
                account = accounts_by_dropdown_id.get(str(int(sk)))
            if account is None:
                return
            comp_name.value = str((account.get("company") or "")).strip()
            manager_name.value = str((account.get("manager") or "")).strip()
            email_field.value = str((account.get("email") or "")).strip()
            phone_field.value = str((account.get("phone") or "")).strip()
            password.value = ""
            self.page.update()

        def on_quick_account_change(e=None):
            v = None
            try:
                if e is not None:
                    ctl = getattr(e, "control", None)
                    if ctl is not None:
                        cv = getattr(ctl, "value", None)
                        if cv is not None:
                            v = cv
                if v is None:
                    v = quick_dd.value
            except Exception:
                v = getattr(quick_dd, "value", None)
            apply_quick_slot_selection(str(v if v is not None else "").strip())

        quick_dd = ft.Dropdown(
            label=self.t("auth.login.account_label", "Акаунт"),
            border_radius=10,
            # Narrowed from 370 to make room for the delete button beside it
            # within the same 370px-wide card — at the full 370 the two
            # together overflowed the card's content width, and the button's
            # hit area ended up outside the parent's layout bounds, silently
            # eating every click on it.
            width=326,
            options=account_options,
            value=default_key,
            on_select=on_quick_account_change,
        )

        def remove_one_quick_account(_):
            sk = str(getattr(quick_dd, "value", "") or "").strip()
            if not sk or sk == NEW_ACCOUNT_KEY:
                self._show_snack(
                    self.t(
                        "auth.login.quick_delete_none_selected",
                        "Оберіть збережений акаунт для видалення",
                    ),
                    "orange",
                )
                return
            target = accounts_by_dropdown_id.get(sk)
            if target is None:
                return
            target_key = _acct_dedupe_key(target)
            all_accounts = self._read_quick_accounts()
            remaining = [
                a
                for a in all_accounts
                if not (isinstance(a, dict) and _acct_dedupe_key(a) == target_key)
            ]
            prev_payload = self._read_quick_accounts_payload()
            prev_sa = (
                bool(prev_payload.get("superadmin_enabled"))
                if isinstance(prev_payload, dict)
                else False
            )
            self._save_quick_accounts(remaining, superadmin_enabled=prev_sa)
            self._show_snack(
                self.t("auth.login.quick_deleted_one", "Акаунт видалено зі списку"),
                "green",
            )
            self.show_login()

        remove_one_quick_btn = ft.IconButton(
            icon=ft.Icons.DELETE_OUTLINE,
            icon_size=18,
            icon_color=ft.Colors.RED_400,
            tooltip=self.t(
                "auth.login.quick_delete_one_tooltip",
                "Видалити обраний акаунт зі списку",
            ),
            on_click=remove_one_quick_account,
        )
        quick_account_selector = ft.Container(
            visible=True,
            content=ft.Column(
                [
                    ft.Row(
                        [quick_dd, remove_one_quick_btn],
                        spacing=2,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=4,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        def clear_saved_quick_accounts(_):
            self._save_quick_accounts([], superadmin_enabled=False)
            self._show_snack(
                self.t(
                    "auth.login.quick_cleared",
                    "Швидкий вхід очищено. Введіть дані вручну та увійдіть знову.",
                ),
                "green",
            )
            self.show_login()

        clear_quick_btn = ft.TextButton(
            self.t("auth.login.clear_quick_accounts", "Очистити збережені акаунти"),
            on_click=clear_saved_quick_accounts,
        )

        self.page.add(
            ft.Stack(
                [
                    ft.Column(
                        [
                            ft.Container(
                                content=ft.Column(
                                    [
                                        ft.Row(
                                            [
                                                ft.Text(
                                                    "CatMan Pro",
                                                    size=32,
                                                    weight="bold",
                                                    color=ft.Colors.BLUE_900,
                                                ),
                                                ft.IconButton(
                                                    icon=ft.Icons.HELP_OUTLINE,
                                                    icon_size=20,
                                                    tooltip=self.t("auth.help.tooltip", "Довідка"),
                                                    on_click=lambda _e: self._show_login_help(),
                                                ),
                                                *(
                                                    [
                                                        ft.IconButton(
                                                            icon=ft.Icons.SETTINGS_OUTLINED,
                                                            icon_size=20,
                                                            tooltip=self.t(
                                                                "auth.login.settings_tooltip",
                                                                "Налаштування (папка кешу)",
                                                            ),
                                                            on_click=lambda _e: self.on_settings_click(),
                                                        )
                                                    ]
                                                    if self.on_settings_click
                                                    else []
                                                ),
                                            ],
                                            spacing=2,
                                            tight=True,
                                            alignment=ft.MainAxisAlignment.CENTER,
                                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        ),
                                        ft.Text(
                                            self.t("auth.login.subtitle", "Вхід у систему"),
                                            size=18, color=ft.Colors.GREY_700,
                                        ),
                                        quick_account_selector,
                                        ft.Container(
                                            content=clear_quick_btn,
                                            visible=has_saved_quick,
                                            alignment=ft.Alignment(0, 0),
                                        ),
                                        ft.Divider(height=10, color=ft.Colors.TRANSPARENT),
                                        comp_name,
                                        manager_name,
                                        email_field,
                                        phone_field,
                                        password,
                                        ft.Divider(height=5, color=ft.Colors.TRANSPARENT),
                                        error_message,  # Напис з'явиться тут
                                        ft.Divider(height=5, color=ft.Colors.TRANSPARENT),
                                        ft.FilledButton(
                                            self.t("auth.login.submit", "УВІЙТИ"),
                                            on_click=handle_login_click,
                                            width=float("inf"),
                                            height=50,
                                            style=ft.ButtonStyle(
                                                shape=ft.RoundedRectangleBorder(radius=10)
                                            ),
                                        ),
                                        *(
                                            [
                                                ft.TextButton(
                                                    self.t(
                                                        "auth.login.go_register",
                                                        "Створити новий акаунт (Реєстрація)",
                                                    ),
                                                    on_click=self.show_register,
                                                )
                                            ]
                                            if self.allow_registration
                                            else []
                                        ),
                                    ],
                                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                padding=40,
                                width=450,
                            )
                        ],
                        scroll=ft.ScrollMode.ADAPTIVE,
                        expand=True,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(
                        content=i18n.build_language_switcher(
                            self.db, self._on_session_language_select
                        ),
                        top=16,
                        right=16,
                    ),
                ],
                expand=True,
            )
        )
        apply_quick_slot_selection(default_key)
        try:
            quick_dd.value = default_key
        except Exception:
            pass
        password.autofocus = bool(
            (comp_name.value or "").strip() and (manager_name.value or "").strip()
        )
        self.page.update()
        self._start_i18n_seed_background()
        self._start_quick_role_sync_background()

    def _start_quick_role_sync_background(self) -> None:
        """Refresh quick_accounts.json role/company labels from Postgres in
        the background. This used to run synchronously at the top of
        show_login(), before the form was built — up to 10 saved accounts
        each cost 1-2 DB round trips, so with several saved accounts that
        alone could add seconds of black screen before anything painted.
        The labels shown right now come from the on-disk cache (immediate
        fallback, same as i18n); the fresh values just land in the file for
        the next time the login screen is rendered."""
        async def _sync():
            try:
                bg_db = DataManager()
                await asyncio.to_thread(self._sync_quick_account_roles_from_db, bg_db)
            except Exception:
                logger.exception("Синхронізація ролей quick_accounts з БД не вдалася")

        try:
            self.page.run_task(_sync)
        except Exception:
            logger.exception("Failed to schedule background quick-account role sync")

    def _start_i18n_seed_background(self) -> None:
        """Kick off translation seeding (default languages + help + UI
        strings) in the background, once per process — now that the login
        form is already visible, this no longer delays first paint."""
        if self._i18n_seed_started:
            return
        self._i18n_seed_started = True

        async def _seed():
            import asyncio

            def _run():
                i18n.seed_default_languages_if_empty(self.db)
                i18n.seed_help_translations_if_new(self.db)
                i18n.seed_ui_translations_if_new(self.db)

            try:
                await asyncio.to_thread(_run)
            except Exception:
                logger.exception("Background i18n seeding failed")

        try:
            self.page.run_task(_seed)
        except Exception:
            logger.exception("Failed to schedule background i18n seeding")

    def _apply_quick_account(
        self, acc, comp_name, manager_name, email_field, phone_field, password_field
    ):
        comp_name.value = str((acc or {}).get("company") or "").strip()
        manager_name.value = str((acc or {}).get("manager") or "").strip()
        email_field.value = str((acc or {}).get("email") or "").strip()
        phone_field.value = str((acc or {}).get("phone") or "").strip()
        password_field.value = ""
        self.page.update()

    def _build_choice_card(self, *, icon, title, subtitle, on_click):
        """Reusable clickable card for the two-step registration picker
        (Компанія/Індивідуал, then VPS/Локал)."""
        return ft.Card(
            content=ft.Container(
                content=ft.Column(
                    [
                        ft.Icon(icon, size=32, color=ft.Colors.PRIMARY),
                        ft.Text(
                            title, size=18, weight="bold", text_align=ft.TextAlign.CENTER
                        ),
                        ft.Text(
                            subtitle,
                            size=12,
                            color="grey700",
                            text_align=ft.TextAlign.CENTER,
                        ),
                    ],
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                ),
                padding=20,
                ink=True,
                on_click=on_click,
                border_radius=12,
            ),
        )

    def _register_shell(self, *, title_row, body_controls, width=640):
        """Common page chrome (language switcher + centered card column) shared
        by every step of the multi-step registration flow."""
        self.page.clean()
        self.page._active_auth_screen = "register"
        self.page.add(
            ft.Stack(
                [
                    ft.Column(
                        [
                            ft.Container(
                                content=ft.Column(
                                    [title_row, ft.Divider(height=16, color="transparent")]
                                    + body_controls,
                                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                width=width,
                            )
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        scroll=ft.ScrollMode.ADAPTIVE,
                        expand=True,
                    ),
                    ft.Container(
                        content=i18n.build_language_switcher(
                            self.db, self._on_session_language_select
                        ),
                        top=16,
                        right=16,
                    ),
                ],
                expand=True,
            )
        )
        self.page.update()

    def show_register(self, e=None):
        """Крок 1 реєстрації: Компанія чи Індивідуал."""

        def pick_company(_):
            self.show_register_storage_choice("company")

        def pick_individual(_):
            self.show_register_storage_choice("individual")

        self._register_shell(
            title_row=ft.Column(
                [
                    ft.Text(
                        self.t("auth.register.title", "Реєстрація в CatMan Pro"),
                        size=28,
                        weight="bold",
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        self.t(
                            "auth.register.choice_kind_subtitle",
                            "Оберіть, як ви будете користуватись програмою",
                        ),
                        size=14,
                        color="grey700",
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=4,
            ),
            body_controls=[
                ft.ResponsiveRow(
                    [
                        ft.Container(
                            self._build_choice_card(
                                icon=ft.Icons.APARTMENT,
                                title=self.t("auth.register.kind_company", "Компанія"),
                                subtitle=self.t(
                                    "auth.register.kind_company_desc",
                                    "Для команди з кількома користувачами. Адмін керує "
                                    "користувачами, регіонами та категоріями.",
                                ),
                                on_click=pick_company,
                            ),
                            col={"xs": 12, "sm": 6},
                        ),
                        ft.Container(
                            self._build_choice_card(
                                icon=ft.Icons.PERSON,
                                title=self.t("auth.register.kind_individual", "Індивідуал"),
                                subtitle=self.t(
                                    "auth.register.kind_individual_desc",
                                    "Для одного користувача. Ви одразу і адмін, і "
                                    "користувач — усі налаштування задаєте самі.",
                                ),
                                on_click=pick_individual,
                            ),
                            col={"xs": 12, "sm": 6},
                        ),
                    ],
                ),
                ft.Divider(height=20, color="transparent"),
                ft.TextButton(
                    self.t("auth.register.go_login", "Вже є акаунт? Увійти"),
                    on_click=self.show_login,
                ),
            ],
        )

    def show_register_storage_choice(self, account_kind: str):
        """Крок 2: VPS (хмара) чи Локал (цей пристрій)."""

        def pick(storage_mode):
            def _handler(_):
                if account_kind == "individual":
                    self.show_register_individual_form(storage_mode)
                else:
                    self.show_register_company_form(storage_mode)

            return _handler

        kind_label = (
            self.t("auth.register.kind_company", "Компанія")
            if account_kind == "company"
            else self.t("auth.register.kind_individual", "Індивідуал")
        )

        self._register_shell(
            title_row=ft.Row(
                [
                    ft.IconButton(
                        icon=ft.Icons.ARROW_BACK,
                        tooltip=self.t("auth.common.back", "Назад"),
                        on_click=lambda _: self.show_register(),
                    ),
                    ft.Text(
                        f"{kind_label} · "
                        + self.t("auth.register.choice_storage_title", "Де зберігати дані?"),
                        size=22,
                        weight="bold",
                    ),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            body_controls=[
                ft.ResponsiveRow(
                    [
                        ft.Container(
                            self._build_choice_card(
                                icon=ft.Icons.CLOUD,
                                title=self.t("auth.register.storage_vps", "Хмара (VPS)"),
                                subtitle=self.t(
                                    "auth.register.storage_vps_desc",
                                    "Дані зберігаються на наших серверах. Доступ з будь-якого "
                                    "пристрою, дані завжди синхронізовані.",
                                ),
                                on_click=pick("vps"),
                            ),
                            col={"xs": 12, "sm": 6},
                        ),
                        ft.Container(
                            self._build_choice_card(
                                icon=ft.Icons.COMPUTER,
                                title=self.t("auth.register.storage_local", "Локально"),
                                subtitle=self.t(
                                    "auth.register.storage_local_desc",
                                    "Дані зберігаються тільки на цьому комп'ютері. Дешевше, "
                                    "працює без інтернету. Доступ лише з цього пристрою — "
                                    "перенести дані на інший пристрій або перейти у Хмару "
                                    "можна пізніше в налаштуваннях.",
                                ),
                                on_click=pick("local"),
                            ),
                            col={"xs": 12, "sm": 6},
                        ),
                    ],
                ),
            ],
        )

    def show_register_company_form(self, storage_mode: str = "vps"):
        """Крок 3 (Компанія): існуюча повна форма реєстрації."""
        self.page.clean()
        self.page._active_auth_screen = "register"

        manager_name = ft.TextField(
            label=self.t("auth.common.name_label", "Ваше Ім'я"), border_radius=10
        )
        manager_email = ft.TextField(
            label=self.t("auth.common.email_label", "E-mail"),
            border_radius=10, prefix_icon="email",
        )
        manager_phone = ft.TextField(
            label=self.t("auth.common.phone_label", "Номер телефону +380"),
            border_radius=10, prefix_icon="phone",
        )
        manager_role = ft.Dropdown(
            label=self.t("auth.register.role_label", "Посада"),
            options=[
                ft.dropdown.Option(
                    key=r, text=self.t(f"auth.role.{self._normalize_role_key(r)}", r)
                )
                for r in self.roles
            ],
            value=self.roles[0],
            border_radius=10,
        )
        company_name = ft.TextField(
            label=self.t("auth.common.company_label", "Назва компанії"), border_radius=10
        )
        password_field = ft.TextField(
            label=self.t("auth.register.password_label", "Пароль (4-8 символів)"),
            password=True,
            can_reveal_password=True,
            border_radius=10,
            max_length=8,
        )

        regions_checks_block = ft.Column(
            spacing=4, height=150, scroll=ft.ScrollMode.ADAPTIVE
        )
        regions_card_hint = ft.Text(
            self.t("auth.register.regions_hint", "Оберіть регіони доступу (можна декілька)"),
            size=12, color="grey700",
        )
        selected_region_ids = []
        region_checks = {}

        def refresh_regions_selector():
            nonlocal selected_region_ids, region_checks
            region_checks = {}
            regions_checks_block.controls.clear()
            selected_region_ids = []

            is_super_admin = manager_role.value == "СуперАдмін"
            if is_super_admin:
                regions_checks_block.controls.append(
                    ft.Text(
                        self.t(
                            "auth.register.sa_no_region_limit",
                            "Для СуперАдміна доступ до регіонів не обмежується",
                        ),
                        size=12,
                        color="grey600",
                    )
                )
                regions_card_hint.value = self.t(
                    "auth.register.sa_no_region_limit_hint",
                    "Для СуперАдміна обмеження регіонів не застосовується",
                )
                self.page.update()
                return

            c_name = (company_name.value or "").strip()
            if not c_name:
                regions_checks_block.controls.append(
                    ft.Text(
                        self.t("auth.register.enter_company_first", "Спочатку введіть назву компанії"),
                        size=12, color="grey600",
                    )
                )
                regions_card_hint.value = self.t(
                    "auth.register.regions_hint", "Оберіть регіони доступу (можна декілька)"
                )
                self.page.update()
                return

            companies = self.db.get_all_companies()
            company = next(
                (
                    c
                    for c in companies
                    if isinstance(c, dict)
                    and (c.get("name") or "").strip().lower() == c_name.lower()
                ),
                None,
            )
            if not company or not company.get("id"):
                regions_checks_block.controls.append(
                    ft.Text(
                        self.t(
                            "auth.register.company_not_found",
                            "Компанію не знайдено. Після створення компанії регіони будуть доступні для вибору.",
                        ),
                        size=12,
                        color="grey600",
                    )
                )
                regions_card_hint.value = self.t(
                    "auth.register.regions_hint", "Оберіть регіони доступу (можна декілька)"
                )
                self.page.update()
                return

            regions = self.db.get_company_regions(company.get("id"))
            if not regions:
                regions_checks_block.controls.append(
                    ft.Text(
                        self.t("auth.register.no_regions", "У компанії ще немає регіонів"),
                        size=12, color="grey600",
                    )
                )
                regions_card_hint.value = self.t(
                    "auth.register.regions_hint", "Оберіть регіони доступу (можна декілька)"
                )
                self.page.update()
                return

            for reg in regions:
                if not isinstance(reg, dict) or not reg.get("id"):
                    continue
                rid = reg.get("id")
                cb = ft.Checkbox(
                    label=reg.get("name") or self.t("auth.register.region_fallback", "Регіон"),
                    value=False,
                )
                region_checks[rid] = cb
                regions_checks_block.controls.append(cb)

            regions_card_hint.value = self.t(
                "auth.register.regions_hint_none_selected",
                "Якщо не обрати жодного регіону - користувач матиме доступ до всіх регіонів компанії",
            )
            self.page.update()

        def on_role_change(e):
            is_super_admin = manager_role.value == "СуперАдмін"
            company_name.disabled = is_super_admin
            if is_super_admin:
                company_name.value = ""
                company_name.hint_text = self.t(
                    "auth.register.company_hint_sa", "Для СуперАдміна не заповнюється"
                )
            else:
                company_name.hint_text = None
            refresh_regions_selector()
            self.page.update()

        manager_role.on_select = on_role_change
        company_name.on_blur = lambda _: refresh_regions_selector()
        on_role_change(None)

        def handle_reg_save(e):
            manager_name.error_text = None
            manager_email.error_text = None
            manager_phone.error_text = None
            company_name.error_text = None
            password_field.error_text = None

            error = False
            if not (manager_name.value or "").strip():
                manager_name.error_text = self.t("auth.err.missing_name", "Не введено: Ім'я")
                error = True
            if not (manager_email.value or "").strip():
                manager_email.error_text = self.t("auth.err.missing_email", "Не введено: E-mail")
                error = True
            if not (manager_phone.value or "").strip():
                manager_phone.error_text = self.t("auth.err.missing_phone", "Не введено: Телефон")
                error = True
            is_super_admin = manager_role.value == "СуперАдмін"

            if not (company_name.value or "").strip() and not is_super_admin:
                company_name.error_text = self.t(
                    "auth.err.missing_company", "Не введено: Назва компанії"
                )
                error = True
            if not (4 <= len(password_field.value) <= 8):
                password_field.error_text = self.t(
                    "auth.err.missing_password_len", "Пароль: від 4 до 8 символів"
                )
                error = True

            if error:
                self._show_snack(
                    self.t(
                        "auth.register.err_check_fields",
                        "Перевірте незаповнені поля",
                    ),
                    "orange",
                )
                self.page.update()
                return

            payload = {
                "manager": manager_name.value,
                "email": manager_email.value,
                "phone": manager_phone.value,
                "role": manager_role.value,
                "company": "" if is_super_admin else company_name.value,
                "password": password_field.value,
                "storage_mode": storage_mode,
                "allowed_region_ids": [
                    rid for rid, cb in region_checks.items() if cb.value
                ],
                "region_data": [],
            }

            if self.db.save_setup(payload):
                self._upsert_quick_account(
                    payload.get("company"),
                    payload.get("manager"),
                    payload.get("email"),
                    payload.get("phone"),
                )
                self._show_snack(
                    self.t("auth.register.submitted", "Заявка на реєстрацію надіслана!"), "green"
                )
                self.show_login()
            else:
                self._show_snack(self.t("auth.register.failed", "Помилка при реєстрації"), "red")

        storage_badge = (
            self.t("auth.register.storage_local", "Локально")
            if storage_mode == "local"
            else self.t("auth.register.storage_vps", "Хмара (VPS)")
        )

        self.page.add(
            ft.Stack(
                [
                    ft.Column(
                        [
                            ft.Container(
                                content=ft.Column(
                                    [
                                        ft.Row(
                                            [
                                                ft.IconButton(
                                                    icon=ft.Icons.ARROW_BACK,
                                                    tooltip=self.t("auth.common.back", "Назад"),
                                                    on_click=lambda _: self.show_register_storage_choice(
                                                        "company"
                                                    ),
                                                ),
                                                ft.Text(
                                                    self.t(
                                                        "auth.register.title",
                                                        "Реєстрація в CatMan Pro",
                                                    ),
                                                    size=28, weight="bold",
                                                ),
                                                ft.IconButton(
                                                    icon=ft.Icons.HELP_OUTLINE,
                                                    icon_size=20,
                                                    tooltip=self.t("auth.help.tooltip", "Довідка"),
                                                    on_click=lambda _e: self._show_register_help(),
                                                ),
                                            ],
                                            spacing=2,
                                            tight=True,
                                            alignment=ft.MainAxisAlignment.CENTER,
                                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                        ),
                                        ft.Container(
                                            content=ft.Text(
                                                f"{self.t('auth.register.kind_company', 'Компанія')} · {storage_badge}",
                                                size=12,
                                                color="grey700",
                                            ),
                                            alignment=ft.Alignment(0, 0),
                                        ),
                                        ft.Card(
                                            ft.Container(
                                                padding=20,
                                                content=ft.Column(
                                                    [
                                                        ft.Text(
                                                            self.t("auth.register.profile", "Профіль"),
                                                            weight="bold",
                                                        ),
                                                        manager_name,
                                                        manager_email,
                                                        manager_phone,
                                                        manager_role,
                                                        company_name,
                                                        password_field,
                                                    ]
                                                ),
                                            )
                                        ),
                                        ft.Card(
                                            ft.Container(
                                                padding=20,
                                                content=ft.Column(
                                                    [
                                                        ft.Text(
                                                            self.t(
                                                                "auth.register.available_regions",
                                                                "Доступні регіони",
                                                            ),
                                                            weight="bold",
                                                        ),
                                                        regions_card_hint,
                                                        regions_checks_block,
                                                        ft.Row(
                                                            [
                                                                ft.TextButton(
                                                                    self.t(
                                                                        "auth.register.refresh_regions",
                                                                        "Оновити список регіонів",
                                                                    ),
                                                                    icon=ft.Icons.REFRESH,
                                                                    on_click=lambda _: refresh_regions_selector(),
                                                                ),
                                                            ],
                                                            wrap=True,
                                                        ),
                                                    ],
                                                    spacing=8,
                                                ),
                                            )
                                        ),
                                        ft.Text(
                                            self.t(
                                                "auth.register.regions_note",
                                                "Регіони та рівні цін задаються централізовано СуперАдміном у керуванні компанією.",
                                            ),
                                            size=12,
                                            color="grey700",
                                            text_align=ft.TextAlign.CENTER,
                                        ),
                                        ft.Divider(height=20, color="transparent"),
                                        ft.FilledButton(
                                            self.t("auth.register.submit", "ЗАРЕЄСТРУВАТИСЯ"),
                                            on_click=handle_reg_save,
                                            width=float("inf"),
                                            height=50,
                                        ),
                                        ft.TextButton(
                                            self.t("auth.register.go_login", "Вже є акаунт? Увійти"),
                                            on_click=self.show_login,
                                        ),
                                    ],
                                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                width=600,
                            )
                        ],
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        scroll=ft.ScrollMode.ADAPTIVE,
                        expand=True,
                    ),
                    ft.Container(
                        content=i18n.build_language_switcher(
                            self.db, self._on_session_language_select
                        ),
                        top=16,
                        right=16,
                    ),
                ],
                expand=True,
            )
        )
        self.page.update()

    def show_register_individual_form(self, storage_mode: str = "vps"):
        """Крок 3 (Індивідуал): спрощена форма — без назви компанії, ролі й регіонів."""
        self.page.clean()
        self.page._active_auth_screen = "register"

        manager_name = ft.TextField(
            label=self.t("auth.common.name_label", "Ваше Ім'я"), border_radius=10
        )
        manager_email = ft.TextField(
            label=self.t("auth.common.email_label", "E-mail"),
            border_radius=10, prefix_icon="email",
        )
        manager_phone = ft.TextField(
            label=self.t("auth.common.phone_label", "Номер телефону +380"),
            border_radius=10, prefix_icon="phone",
        )
        password_field = ft.TextField(
            label=self.t("auth.register.password_label", "Пароль (4-8 символів)"),
            password=True,
            can_reveal_password=True,
            border_radius=10,
            max_length=8,
        )

        def handle_reg_save(e):
            manager_name.error_text = None
            manager_email.error_text = None
            manager_phone.error_text = None
            password_field.error_text = None

            error = False
            if not (manager_name.value or "").strip():
                manager_name.error_text = self.t("auth.err.missing_name", "Не введено: Ім'я")
                error = True
            if not (manager_email.value or "").strip():
                manager_email.error_text = self.t("auth.err.missing_email", "Не введено: E-mail")
                error = True
            if not (manager_phone.value or "").strip():
                manager_phone.error_text = self.t("auth.err.missing_phone", "Не введено: Телефон")
                error = True
            if not (4 <= len(password_field.value or "") <= 8):
                password_field.error_text = self.t(
                    "auth.err.missing_password_len", "Пароль: від 4 до 8 символів"
                )
                error = True

            if error:
                self._show_snack(
                    self.t("auth.register.err_check_fields", "Перевірте незаповнені поля"),
                    "orange",
                )
                self.page.update()
                return

            payload = {
                "full_name": manager_name.value,
                "email": manager_email.value,
                "phone": manager_phone.value,
                "password": password_field.value,
                "storage_mode": storage_mode,
            }
            ok, err = self.db.save_individual_setup(payload)
            if not ok:
                self._show_snack(
                    err or self.t("auth.register.failed", "Помилка при реєстрації"), "red"
                )
                return

            # Індивідуал схвалюється миттєво (сам собі адмін) — одразу логінимо,
            # без проміжного "заявку надіслано" екрану.
            try:
                user = self.db.check_auth(
                    "",
                    manager_name.value,
                    password_field.value,
                    manager_email.value,
                    manager_phone.value,
                )
            except Exception:
                logger.exception("Auto-login after individual registration failed")
                user = None

            if user and user.get("is_approved"):
                self._upsert_quick_account(
                    user.get("company_name") or "",
                    manager_name.value,
                    manager_email.value,
                    manager_phone.value,
                    role=(user.get("role") or ""),
                )
                self._show_snack(
                    self.t("auth.register.individual_created", "Акаунт створено!"), "green"
                )
                self.on_success(user)
            else:
                self._show_snack(
                    self.t("auth.register.submitted", "Акаунт створено! Увійдіть, будь ласка."),
                    "green",
                )
                self.show_login()

        storage_badge = (
            self.t("auth.register.storage_local", "Локально")
            if storage_mode == "local"
            else self.t("auth.register.storage_vps", "Хмара (VPS)")
        )

        self._register_shell(
            title_row=ft.Column(
                [
                    ft.Row(
                        [
                            ft.IconButton(
                                icon=ft.Icons.ARROW_BACK,
                                tooltip=self.t("auth.common.back", "Назад"),
                                on_click=lambda _: self.show_register_storage_choice("individual"),
                            ),
                            ft.Text(
                                self.t("auth.register.title", "Реєстрація в CatMan Pro"),
                                size=28,
                                weight="bold",
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.CENTER,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Text(
                        f"{self.t('auth.register.kind_individual', 'Індивідуал')} · {storage_badge}",
                        size=12,
                        color="grey700",
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=4,
            ),
            body_controls=[
                ft.Card(
                    ft.Container(
                        padding=20,
                        content=ft.Column(
                            [
                                ft.Text(self.t("auth.register.profile", "Профіль"), weight="bold"),
                                manager_name,
                                manager_email,
                                manager_phone,
                                password_field,
                            ]
                        ),
                    )
                ),
                ft.Text(
                    self.t(
                        "auth.register.individual_note",
                        "Ви станете адміністратором свого акаунту й самостійно "
                        "налаштуєте регіони, категорії та валюти після входу.",
                    ),
                    size=12,
                    color="grey700",
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Divider(height=20, color="transparent"),
                ft.FilledButton(
                    self.t("auth.register.submit", "ЗАРЕЄСТРУВАТИСЯ"),
                    on_click=handle_reg_save,
                    width=float("inf"),
                    height=50,
                ),
                ft.TextButton(
                    self.t("auth.register.go_login", "Вже є акаунт? Увійти"),
                    on_click=self.show_login,
                ),
            ],
            width=600,
        )

    def _show_help_dialog(self, title: str, body: str) -> None:
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=ft.Container(width=420, content=ft.Text(body, size=13)),
            actions=[
                ft.FilledButton(
                    self.t("common.help.close", "Зрозуміло"),
                    on_click=lambda _e: self.page.pop_dialog(),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _show_login_help(self) -> None:
        self._show_help_dialog(
            self.t("auth.help.login_title", "Довідка — вхід у систему"),
            self.t(
                "auth.help.login_body",
                "Це екран входу в CatMan Pro. Введіть назву компанії (не потрібно лише"
                " СуперАдміну), ваше ім'я, e-mail, телефон і пароль, після чого натисніть"
                " «УВІЙТИ». Якщо ви вже входили раніше, оберіть свій акаунт у списку"
                " «Акаунт» — поля заповняться автоматично. Мову інтерфейсу можна"
                " змінити кнопкою у верхньому правому куті. Немає акаунта — натисніть"
                " «Створити новий акаунт (Реєстрація)».",
            ),
        )

    def _show_register_help(self) -> None:
        self._show_help_dialog(
            self.t("auth.help.register_title", "Довідка — реєстрація"),
            self.t(
                "auth.help.register_body",
                "Тут створюється заявка на новий акаунт. Заповніть профіль (ім'я, e-mail,"
                " телефон, посаду, назву компанії, пароль 4-8 символів) і за потреби"
                " оберіть регіони доступу — якщо жодного не обрати, доступ буде до всіх"
                " регіонів компанії. Після натискання «ЗАРЕЄСТРУВАТИСЯ» заявку має"
                " підтвердити адміністратор, перш ніж ви зможете увійти.",
            ),
        )

    def open_storage_settings_dialog(self) -> None:
        """Standalone single-purpose apps (no dashboard/Settings tab around
        them) reach app_storage's cache-relocation setting through this
        gear-icon dialog instead — same underlying app_storage functions
        and flow as views/settings_view.py's Settings tab uses."""
        import app_storage

        path_text = ft.Text(
            app_storage.get_current_storage_dir(), size=12,
            color=ft.Colors.GREY_700, selectable=True,
        )
        status_text = ft.Text("", size=11)
        busy = [False]

        async def _pick_folder(_e=None):
            if busy[0]:
                return
            fp = ft.FilePicker()
            self.page.services.append(fp)
            self.page.update()
            await asyncio.sleep(0.2)
            new_dir = await fp.get_directory_path(
                dialog_title=self.t(
                    "auth.settings.pick_folder_title", "Оберіть папку для збереження кешу"
                ),
            )
            try:
                self.page.services.remove(fp)
                self.page.update()
            except Exception:
                pass
            if not new_dir:
                return
            busy[0] = True
            status_text.value = self.t("auth.settings.moving", "Переносимо файли...")
            status_text.color = ft.Colors.GREY_700
            try:
                status_text.update()
            except Exception:
                pass
            ok, msg = await asyncio.to_thread(app_storage.set_storage_dir, new_dir)
            busy[0] = False
            status_text.value = msg
            status_text.color = "#15803D" if ok else "#DC2626"
            if ok:
                path_text.value = app_storage.get_current_storage_dir()
            try:
                path_text.update()
                status_text.update()
            except Exception:
                pass

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(self.t("auth.settings.title", "Налаштування")),
            content=ft.Container(
                width=420,
                content=ft.Column(
                    [
                        ft.Text(
                            self.t(
                                "auth.settings.storage_hint",
                                "Тут кешуються локальні файли цього застосунку на цьому"
                                " пристрої. Поточна папка:",
                            ),
                            size=12,
                            color=ft.Colors.GREY_700,
                        ),
                        path_text,
                        ft.FilledButton(
                            self.t("auth.settings.pick_folder", "Обрати іншу папку"),
                            on_click=_pick_folder,
                        ),
                        status_text,
                    ],
                    tight=True,
                    spacing=8,
                ),
            ),
            actions=[
                ft.FilledButton(
                    self.t("common.help.close", "Зрозуміло"),
                    on_click=lambda _e: self.page.pop_dialog(),
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.show_dialog(dlg)

    def _show_snack(self, text, color):
        bar = ft.SnackBar(ft.Text(text), bgcolor=color)
        try:
            # Preferred path on newer Flet builds.
            self.page.show_snack_bar(bar)
            return
        except Exception:
            pass
        try:
            # Backward-compatible path.
            bar.open = True
            self.page.snack_bar = bar
            self.page.update()
        except Exception:
            logger.exception("Не вдалося показати Snackbar повідомлення")

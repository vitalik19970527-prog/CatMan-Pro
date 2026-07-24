"""Shared help-popup utilities for views and modal dialogs."""
from __future__ import annotations
import contextlib
import flet as ft


def make_help_title(title_text: str, on_help_click) -> ft.Row:
    """Return a Row suitable for AlertDialog.title: text + ? icon button."""
    return ft.Row(
        [
            ft.Text(title_text, weight=ft.FontWeight.W_700, expand=True),
            ft.IconButton(
                icon=ft.Icons.HELP_OUTLINE,
                icon_size=16,
                tooltip="Довідка",
                on_click=on_help_click,
            ),
        ],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        tight=True,
    )


def show_modal_help(page, title: str, items: list[str | tuple]) -> None:
    """Show a scrollable help popup.

    items — list of either:
      - str  → plain description line
      - ("header", None)  → bold section header
      - ("label", "desc") → bold label + description
    """
    if not page:
        return

    controls = []
    for item in items:
        if isinstance(item, str):
            controls.append(ft.Text(item, size=12, color=ft.Colors.BLUE_GREY_800))
        elif isinstance(item, tuple) and len(item) == 2:
            lbl, desc = item
            if desc is None:
                controls.append(
                    ft.Text(lbl, size=13, weight=ft.FontWeight.W_700)
                )
            elif lbl:
                controls.append(
                    ft.Text(f"{lbl}: {desc}", size=12, color=ft.Colors.BLUE_GREY_800)
                )
            else:
                controls.append(
                    ft.Text(desc, size=12, color=ft.Colors.BLUE_GREY_800)
                )

    def _sep():
        return ft.Divider(height=8, color=ft.Colors.BLUE_GREY_100)

    final = []
    for ctrl in controls:
        final.append(ctrl)
    # insert dividers after bold headers
    spaced: list = []
    for i, ctrl in enumerate(final):
        spaced.append(ctrl)
        if (
            isinstance(ctrl, ft.Text)
            and ctrl.weight == ft.FontWeight.W_700
            and i < len(final) - 1
        ):
            spaced.append(_sep())

    dlg = ft.AlertDialog(
        modal=True,
        title=ft.Text(title, size=14, weight=ft.FontWeight.W_700),
        content=ft.Container(
            width=440,
            height=min(480, max(120, len(spaced) * 26 + 40)),
            content=ft.Column(spaced, spacing=6, scroll=ft.ScrollMode.ADAPTIVE),
        ),
        actions=[
            ft.FilledButton("Зрозуміло", on_click=lambda _: page.close(dlg)),
        ],
        actions_alignment=ft.MainAxisAlignment.END,
    )
    with contextlib.suppress(Exception):
        page.show_dialog(dlg)


def vps_migration_help_items() -> list[str | tuple]:
    """Help content for the 'Налаштування інфраструктури' section (перенесення
    БД на VPS або на власний сервер компанії) — written for someone with zero
    technical background. Shared between admin_dashboard.py and
    superadmin_dashboard.py so the wording never drifts between the two."""
    return [
        ("Що це таке?", None),
        "Зараз усі дані (товари, замовлення тощо) зберігаються в базі даних "
        "PostgreSQL на цьому комп'ютері. Це поле дозволяє перенести дані на "
        "інший сервер — орендований в інтернеті (VPS) або власний сервер "
        "компанії — щоб дані були доступні звідусіль і сервер завжди був онлайн.",

        ("Варіант 1 — орендувати VPS", None),
        "1. Орендуйте VPS у будь-якого провайдера (наприклад DigitalOcean, "
        "Hetzner, timeweb, Selectel). Досить найдешевшого тарифу з Ubuntu. "
        "Після оплати вам дадуть: IP-адресу сервера, логін (зазвичай root) і пароль.",
        "2. Підключіться до сервера по SSH (на Windows — програма PuTTY; "
        "вводите IP, логін root і пароль) і встановіть базу даних:\n"
        "sudo apt update && sudo apt install postgresql -y",
        "3. Створіть базу і користувача (там же, в терміналі):\n"
        "sudo -u postgres psql\n"
        "CREATE DATABASE catman;\n"
        "CREATE USER catman_user WITH PASSWORD 'ваш_надійний_пароль';\n"
        "GRANT ALL PRIVILEGES ON DATABASE catman TO catman_user;\n"
        "\\q",
        "4. Дозвольте підключення ззовні (за замовчуванням Postgres приймає "
        "з'єднання лише з самого сервера). У файлі postgresql.conf знайдіть "
        "listen_addresses і встановіть listen_addresses = '*'. У файлі "
        "pg_hba.conf додайте рядок: host all all 0.0.0.0/0 md5. Потім: "
        "sudo systemctl restart postgresql",
        "5. Відкрийте порт 5432: sudo ufw allow 5432 — а також, якщо у "
        "провайдера є окрема вкладка \"Firewall\"/\"Security Group\" в його "
        "панелі керування, дозвольте порт 5432 і там теж.",
        "6. Зберіть рядок підключення (DSN) за зразком: "
        "postgresql://USER:PASSWORD@HOST:5432/DBNAME — тобто "
        "postgresql://catman_user:ваш_пароль@IP_СЕРВЕРА:5432/catman",
        "7. Вставте цей рядок у поле \"VPS БД (DSN)\" нижче, натисніть "
        "\"Зберегти\", а потім \"Копіювати і переключити\" — застосунок сам "
        "скопіює всі поточні дані на новий сервер і перемкнеться на нього. "
        "Стара база НІЧОГО не втрачає — дані видаляються з неї лише окремою "
        "кнопкою \"Видалити дані зі старої БД\", і тільки коли ви самі це натиснете.",

        ("Варіант 2 — власний сервер компанії замість VPS", None),
        "Так, це реально — механізм абсолютно той самий. Головна вимога: цей "
        "сервер має бути ПОСТІЙНО увімкнений і мати встановлений PostgreSQL "
        "(ті самі кроки 2-6 вище).",
        "Якщо всі користувачі працюють в тому самому офісі (одна локальна "
        "мережа) — все просто: замість IP з інтернету використовуєте "
        "внутрішню адресу сервера (напр. 192.168.1.50).",
        "Якщо потрібен доступ ЗВІДКИСЬ ІЗ ІНШОГО МІСЦЯ (віддалені менеджери, "
        "інший офіс) — офісний роутер має або (а) пробросити порт 5432 назовні "
        "в інтернет (і бажано мати статичну публічну IP-адресу від "
        "провайдера — зі змінною IP з'єднання постійно рватиметься), або "
        "(б) підняти VPN між віддаленим користувачем і офісною мережею. Це "
        "складніше і краще доручити тому, хто обслуговує мережу компанії.",
        "Кожна компанія тут має СВОЄ окреме поле DSN (розділ \"Дані компанії\") "
        "— саме для сценарію \"загальні дані (компанії/менеджери) лишаються "
        "на моєму сервері, а дані конкретної компанії — на її власному "
        "сервері\". Заповніть DSN-поле тільки в розділі цієї компанії, "
        "розділ \"Дані доступу (загальні)\" не чіпайте.",

        ("Важливо перед перенесенням", None),
        "Запишіть десь пароль від нової бази одразу — повторно він ніде не "
        "показується.",
        "Перше збереження нічого не видаляє — спокійно перевірте, що нове "
        "підключення працює, і лише тоді видаляйте дані зі старого місця.",
        "Якщо після збереження застосунок довго \"висить\" або показує "
        "помилку підключення — майже завжди це невірний пароль/IP, або "
        "порт 5432 заблокований фаєрволом (крок 5 вище).",
    ]

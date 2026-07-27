import asyncio
import contextlib

import flet as ft
import i18n

# ---------------------------------------------------------------------------
# Theme colour shims
# ---------------------------------------------------------------------------
_BORDER  = '#EAECEF'
_ACCENT  = '#1D4ED8'
_MUTED   = '#475569'
_BORDER2 = '#D6DCE6'
_PANEL   = '#FFFFFF'


def _th_from_view(view) -> dict:
    from views.themes import get_active_theme
    return get_active_theme(str(getattr(view, 'manager_id', '') or ''))


from logger import logger


def _normalize_role(role):
    return (role or "").strip().lower().replace(" ", "")


def _can_manage_categories(role):
    return _normalize_role(role) in {
        "суперадмін",
        "суперадмин",
        "superadmin",
        "адмін",
        "админ",
        "admin",
    }


class CategoriesBrandsView:
    HEADER_SIZE = 24
    META_TEXT_SIZE = 12
    EMPTY_TEXT_SIZE = 14
    _expanded_state_by_context = {}
    # Archive-section collapse toggles are separate from the main category/
    # level tree above — persisted the same way (keyed by the same per-user
    # context) so re-instantiating this view (every tab load / sync pulse)
    # doesn't silently re-collapse them.
    _archive_collapsed_by_context: dict = {}
    _level_archive_expanded_by_context: dict = {}

    def __init__(
        self,
        content_area,
        user_data,
        db,
        current_region_name,
        catalog_config=None,
        quick_filter: str = "",
    ):
        self.content_area = content_area
        self.user_data = user_data if isinstance(user_data, dict) else {}
        self.db = db
        self.company_id = self.user_data.get("company_id")
        self.manager_id = (
            self.user_data.get("active_manager_id")
            or self.user_data.get("id")
            or self.user_data.get("$id")
            or ""
        )
        self.t = i18n.bind(str(self.manager_id or ""))
        self.current_region_name = current_region_name
        self.quick_filter = str(quick_filter or "").strip().lower()
        self.bypass_category_access = (
            bool(self.user_data.get("bypass_category_access"))
            or self.user_data.get("role") == "СуперАдмін"
        )
        self.superadmin_explicit_mode = bool(
            self.user_data.get("superadmin_explicit_mode")
        )
        self.can_manage_categories = _can_manage_categories(self.user_data.get("role"))
        self.read_only_subordinate = bool(
            self.user_data.get("subordinate_view_readonly")
        )
        self._general_mode: bool = bool(self.user_data.get("general_mode", False))
        self._general_manager_ids: list[str] = list(
            self.user_data.get("general_manager_ids") or []
        )
        from views.general_mode_mixin import build_general_manager_names

        self._general_manager_names: dict[str, str] = build_general_manager_names(
            self.user_data.get("all_subordinates_flat") or [], self.user_data
        )
        self.current_categories = []
        self._all_subs_map = {}
        self._all_series_map = {}
        self._all_brands_map = {}
        self.tm_types = [
            "Загальна торгова марка",
            "Ексклюзивна торгова марка",
            "Власна торгова марка",
        ]
        self._debounce_tasks = {}
        self._expanded_category_ids = set()
        self._expanded_node_ids = set()
        self._visible_scope_node_ids = set()
        self._header_loading_indicator = None

        self._restore_expanded_state()

        raw_regions = (
            self.db.get_company_regions(self.company_id) if self.company_id else []
        )
        self.regions_data = raw_regions if isinstance(raw_regions, list) else []
        logger.info(
            "[categories-debug] init | company_id=%s | manager_id=%s | role=%s | region_name=%s | bypass=%s | readonly=%s | superadmin_explicit=%s | regions=%s",
            self.company_id,
            self.manager_id,
            self.user_data.get("role"),
            self.current_region_name,
            self.bypass_category_access,
            self.read_only_subordinate,
            self.superadmin_explicit_mode,
            len(self.regions_data),
        )

    def _dialog_content_height(self, desired=560, min_height=260):
        try:
            page = self.content_area.page if self.content_area else None
            page_h = getattr(page, "height", None) if page else None
            if not page_h and page:
                win = getattr(page, "window", None)
                page_h = getattr(win, "height", None) if win else None
            if isinstance(page_h, (int, float)) and page_h > 0:
                return max(min_height, min(desired, int(page_h * 0.72)))
        except Exception:
            pass
        return desired

    def _expanded_state_key(self):
        return (
            str(self.company_id or ""),
            str(self.manager_id or ""),
            str(self.current_region_name or ""),
            "1" if self.bypass_category_access else "0",
        )

    def _restore_expanded_state(self):
        try:
            state = CategoriesBrandsView._expanded_state_by_context.get(
                self._expanded_state_key(), {}
            )
            cat_ids = state.get("categories") if isinstance(state, dict) else []
            node_ids = state.get("nodes") if isinstance(state, dict) else []
            self._expanded_category_ids = set(cat_ids or [])
            self._expanded_node_ids = set(node_ids or [])
        except Exception:
            self._expanded_category_ids = set()
            self._expanded_node_ids = set()

    def _save_expanded_state(self):
        try:
            CategoriesBrandsView._expanded_state_by_context[
                self._expanded_state_key()
            ] = {
                "categories": sorted(self._expanded_category_ids),
                "nodes": sorted(self._expanded_node_ids),
            }
        except Exception:
            pass

    def _category_levels_config(self, category_row):
        cfg = (
            self.db.get_category_levels_config(
                category_row if isinstance(category_row, dict) else {}
            )
            or {}
        )
        levels = []
        for row in (cfg.get("levels") if isinstance(cfg.get("levels"), list) else []):
            if not isinstance(row, dict):
                continue
            try:
                pos = int(row.get("position") or 0)
            except Exception:
                continue
            if pos < 1 or pos > 6:
                continue
            levels.append(
                {
                    "position": pos,
                    "title": str(row.get("title") or f"Рівень {pos}").strip()
                    or f"Рівень {pos}",
                    "enabled": bool(row.get("enabled")),
                }
            )
        levels.sort(key=lambda x: int(x.get("position") or 0))

        if not levels:
            # Safety fallback for legacy/broken rows: always show an editable 1..6 schema.
            fallback = self.db.get_category_levels_config(None) or {}
            for row in fallback.get("levels") or []:
                if not isinstance(row, dict):
                    continue
                try:
                    pos = int(row.get("position") or 0)
                except Exception:
                    continue
                if pos < 1 or pos > 6:
                    continue
                levels.append(
                    {
                        "position": pos,
                        "title": str(row.get("title") or f"Рівень {pos}").strip()
                        or f"Рівень {pos}",
                        "enabled": bool(row.get("enabled")),
                    }
                )
            levels.sort(key=lambda x: int(x.get("position") or 0))

        return {
            "levels": levels,
            "use_products": bool(cfg.get("use_products", True)),
        }

    def _clear_field_errors(self, *controls):
        for control in controls:
            if control is not None and hasattr(control, "error_text"):
                control.error_text = None

    def _get_page_safe(self):
        """Safely get page object, returns None if not available."""
        try:
            if self.content_area and hasattr(self.content_area, "page"):
                return self.content_area.page
        except (RuntimeError, AttributeError):
            pass
        return None

    def _show_dialog_safe(self, dialog):
        """Safely show dialog if page is available."""
        try:
            page = self._get_page_safe()
            if page:
                page.show_dialog(dialog)
        except Exception:
            logger.exception("Failed to show dialog")

    def _show_help(self) -> None:
        from views.help_utils import show_modal_help
        page = self._get_page_safe()
        if not page:
            return
        t = self.t
        show_modal_help(page, t("categories.help.title", "Довідка — Категорії та Бренди"), [
            t("categories.help.desc",
              "Ієрархія каталогу: Категорія → Рівні → Підкатегорія → Серія → Бренд."
              " Кожна категорія може мати налаштований ланцюжок рівнів."),
            (t("categories.help.steps_title", "Як користуватись"), None),
            t("categories.help.step1", "1. Пошуковий рядок вгорі — фільтрує категорії та рівні за назвою."),
            t("categories.help.step2", "2. «Категорія» — створити нову категорію з налаштованими рівнями ієрархії."),
            t("categories.help.step3", "3. «Редагувати» поруч з категорією — змінити назву, цільову рент. і рівні."),
            t("categories.help.step4", "4. «Підкатегорія» / «Серія» / «Бренд» — додати відповідний дочірній елемент."),
            t("categories.help.step5", "5. Кнопки 🗑 видаляють елемент після підтвердження."),
            t("categories.help.step6", "6. Кнопки «Рівні» і «Ієрархія» розкривають вкладені елементи категорії."),
        ])

    def _show_snack(self, text, color):
        """Show snack bar message if page is available."""
        try:
            page = self._get_page_safe()
            if page:
                page.snack_bar = ft.SnackBar(
                    ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
                    bgcolor=color,
                    duration=3000,
                )
                page.snack_bar.open = True
                page.update()
        except Exception:
            pass

    def _mark_required(self, control, message="Поле обов'язкове"):
        if control is not None and hasattr(control, "error_text"):
            control.error_text = message

    def _debounce_call(self, key, callback, delay=0.1):
        prev = self._debounce_tasks.get(key)
        if prev and not prev.done():
            prev.cancel()

        async def _runner():
            try:
                await asyncio.sleep(delay)
                callback()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Debounce callback failed")

        self._debounce_tasks[key] = asyncio.create_task(_runner())

    def _region_name_by_id(self, region_id):
        return next(
            (
                region.get("name")
                for region in self.regions_data
                if isinstance(region, dict) and region.get("id") == region_id
            ),
            None,
        )

    def _category_label(self, category):
        category = category if isinstance(category, dict) else {}
        category_name = category.get("name", "Категорія")
        region_name = self._region_name_by_id(category.get("region_id"))
        if region_name:
            return f"{category_name} ({region_name})"
        return category_name

    def _subcategory_label(self, subcategory, category=None):
        subcategory = subcategory if isinstance(subcategory, dict) else {}
        subcategory_name = subcategory.get("name", "Підкатегорія")
        category = category if isinstance(category, dict) else {}
        region_name = self._region_name_by_id(
            category.get("region_id") or subcategory.get("region_id")
        )
        if region_name:
            return f"{subcategory_name} ({region_name})"
        return subcategory_name

    def _brand_label(self, brand, category=None):
        brand = brand if isinstance(brand, dict) else {}
        brand_name = brand.get("name") or ""
        category = category if isinstance(category, dict) else {}
        region_name = self._region_name_by_id(
            category.get("region_id") or brand.get("region_id")
        )
        if region_name:
            return f"{brand_name} ({region_name})"
        return brand_name

    def _series_label(self, series, category=None):
        series = series if isinstance(series, dict) else {}
        series_name = series.get("name", "Серія")
        category = category if isinstance(category, dict) else {}
        region_name = self._region_name_by_id(
            category.get("region_id") or series.get("region_id")
        )
        if region_name:
            return f"{series_name} ({region_name})"
        return series_name

    def build(self):
        _th = _th_from_view(self)
        global _BORDER, _BORDER2, _ACCENT, _MUTED, _PANEL
        _BORDER = _BORDER2 = _th["border"]
        _ACCENT = _th["icon_color"]; _MUTED = _th["muted"]; _PANEL = _th["panel"]
        if self._general_mode and self._general_manager_ids:
            from views.general_wrapper import build_general_sections

            _rid = self._resolve_region_id()
            _cid, _db = self.company_id, self.db
            _bypass = self.bypass_category_access

            def _count_cats(mid, _db=_db, _cid=_cid, _rid=_rid, _bp=_bypass):
                try:
                    if _bp:
                        items = _db.get_company_categories(_cid, _rid) or []
                    else:
                        items = _db.get_accessible_categories(_cid, mid, _rid) or []
                    return len(items)
                except Exception:
                    return 0

            build_general_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={
                    "current_region_name": self.current_region_name,
                    "quick_filter": self.quick_filter,
                },
                count_getter=_count_cats,
                count_label="Категорій:",
            )
            return

        _all_regions = self.current_region_name in {
            None,
            "",
            "__all_regions__",
            "Всі регіони",
        }
        if _all_regions:
            from views.general_wrapper import build_region_sections

            _cid2, _db2, _mid2 = self.company_id, self.db, self.manager_id
            _regions2 = _db2.get_company_regions(_cid2) or []
            _bypass2 = self.bypass_category_access

            def _count_cats_r(rid, _db=_db2, _cid=_cid2, _mid=_mid2, _bp=_bypass2):
                try:
                    if _bp:
                        items = _db.get_company_categories(_cid, rid) or []
                    else:
                        items = _db.get_accessible_categories(_cid, _mid, rid) or []
                    return len(items)
                except Exception:
                    return 0

            build_region_sections(
                self.content_area,
                self.user_data,
                self.db,
                self.__class__,
                view_extra_kwargs={"quick_filter": self.quick_filter},
                count_getter=_count_cats_r,
                region_kwarg_name="current_region_name",
                count_label="Категорій:",
            )
            return

        logger.info("[categories-debug] build start")
        try:
            if (
                not self.content_area
                or not hasattr(self.content_area, "page")
                or not self.content_area.page
            ):
                logger.warning(
                    "[categories-debug] build aborted: content_area/page missing"
                )
                return
        except (RuntimeError, AttributeError):
            logger.warning(
                "[categories-debug] build aborted: content_area/page runtime error"
            )
            return

        self.content_area.controls.clear()

        self.search_field = ft.TextField(
            hint_text=self.t("categories.search_hint", "Пошук по категоріях та рівнях ієрархії"),
            prefix_icon=ft.Icons.SEARCH,
            border_radius=10,
            height=36,
            text_size=12,
            content_padding=ft.padding.symmetric(horizontal=10, vertical=6),
            on_change=lambda _: self._debounce_call(
                "categories_screen_search", self.load_categories
            ),
        )

        self._header_loading_indicator = ft.Row(
            [
                ft.ProgressRing(width=16, height=16, stroke_width=2, color=_ACCENT),
                ft.Text("Завантаження...", size=10, color=_MUTED),
            ],
            spacing=6,
            visible=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        header = ft.Row(
            [
                ft.Text(self.t("categories.title", "Категорії"), size=self.HEADER_SIZE, weight="bold"),
                self._header_loading_indicator,
                ft.IconButton(
                    icon=ft.Icons.HELP_OUTLINE,
                    icon_size=18,
                    tooltip=self.t("common.help.tooltip", "Довідка"),
                    on_click=lambda _e: self._show_help(),
                ),
            ],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        actions_controls = []
        if not self.superadmin_explicit_mode and self.can_manage_categories:
            # Для користувача категорія створюється в його компанії/поточному регіоні автоматично.
            actions_controls.append(
                ft.FilledButton(
                    self.t("categories.add_btn", "Категорія"),
                    icon=ft.Icons.CATEGORY,
                    height=30,
                    style=ft.ButtonStyle(
                        text_style=ft.TextStyle(size=11),
                        padding=ft.padding.symmetric(horizontal=8, vertical=2),
                    ),
                    on_click=lambda _: self.show_category_dialog(),
                )
            )

        actions_row = ft.Row(
            actions_controls,
            spacing=6,
            wrap=True,
            visible=bool(actions_controls),
        )

        # Vertical scroll must be owned by dashboard `content_area`.
        self.categories_list = ft.Column(
            spacing=6,
            scroll=ft.ScrollMode.AUTO,
        )
        self.loading_box = ft.Container(
            padding=14,
            visible=True,
            content=ft.Column(
                [
                    ft.Text(self.t("categories.loading", "Завантаження категорій..."), color="grey700"),
                    ft.ProgressRing(width=22, height=22, stroke_width=3),
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        self.content_area.controls.extend(
            [
                header,
                ft.Text(
                    "Фільтр: Категорії без цільової рентабельності",
                    color="grey700",
                    size=self.META_TEXT_SIZE,
                    visible=self.quick_filter == "missing_target_margin",
                ),
                ft.Text(
                    "Режим: Всі компанії/регіони. Натискайте Редагувати для модалки.",
                    color="grey700",
                    size=self.META_TEXT_SIZE,
                    visible=self.superadmin_explicit_mode and not self.company_id,
                ),
                ft.Text(
                    "Режим перегляду підлеглого: створення і редагування вимкнено",
                    color="grey700",
                    size=self.META_TEXT_SIZE,
                    visible=self.read_only_subordinate,
                ),
                actions_row,
                ft.Row([self.search_field]),
                ft.Divider(height=6, color="transparent"),
                self.loading_box,
                self.categories_list,
            ]
        )
        _pg = self._get_page_safe()
        logger.info(
            "[categories-debug] build controls mounted | controls=%s | page_id=%s",
            len(self.content_area.controls),
            hex(id(_pg)) if _pg else "none",
        )
        self.content_area.update()
        logger.info(
            "[categories-debug] build update complete; scheduling async initial load"
        )
        if _pg:
            _pg.run_task(self._async_initial_load)

    async def _async_initial_load(self):
        logger.info("[categories-debug] async_initial_load start")
        # General mode uses build_general_sections — categories_list doesn't exist.
        if self._general_mode and self._general_manager_ids:
            return
        if not hasattr(self, "categories_list"):
            return
        try:
            if self._header_loading_indicator is not None:
                self._header_loading_indicator.visible = True
            if self.content_area and hasattr(self.content_area, "update"):
                self.content_area.update()
            await asyncio.sleep(0.01)
            self.load_categories()
            if getattr(self, "loading_box", None) is not None:
                self.loading_box.visible = False
            logger.info(
                "[categories-debug] async_initial_load complete | category_controls=%s",
                len(getattr(self.categories_list, "controls", []) or []),
            )
        except Exception as ex:
            logger.exception("[categories-debug] async_initial_load failed: %s", ex)
        finally:
            if self._header_loading_indicator is not None:
                self._header_loading_indicator.visible = False
            if self.content_area and hasattr(self.content_area, "update"):
                self.content_area.update()

    async def resume(self) -> None:
        """Reload categories from DB when switching back to the cached tab.

        Called by the dashboard fast-path so newly created series/categories
        (e.g. created from the Products view) always appear without a manual
        Ctrl+F5 reload.
        """
        await self._async_initial_load()

    def _resolve_region_id(self):
        if self.current_region_name in {None, "", "__all_regions__", "Всі регіони"}:
            logger.info(
                "[categories-debug] resolve_region_id -> ALL | current_region_name=%s",
                self.current_region_name,
            )
            return None
        for region in self.regions_data:
            if (
                isinstance(region, dict)
                and region.get("name") == self.current_region_name
            ):
                logger.info(
                    "[categories-debug] resolve_region_id matched by name | region_name=%s | region_id=%s",
                    self.current_region_name,
                    region.get("id"),
                )
                return region.get("id")
        logger.warning(
            "[categories-debug] resolve_region_id no match | region_name=%s | known_regions=%s",
            self.current_region_name,
            [r.get("name") for r in self.regions_data if isinstance(r, dict)],
        )
        return None

    def _set_loading_text(self, text: str) -> None:
        try:
            ind = self._header_loading_indicator
            if ind is None or not ind.visible:
                return
            ctrls = getattr(ind, "controls", None) or []
            txt = ctrls[1] if len(ctrls) > 1 and isinstance(ctrls[1], ft.Text) else None
            if txt is None:
                return
            txt.value = text
            with contextlib.suppress(Exception):
                txt.update()
        except Exception:
            pass

    def load_categories(self):
        logger.info("[categories-debug] load_categories start")
        self.categories_list.controls.clear()
        region_id = self._resolve_region_id()
        self._visible_scope_node_ids = set()
        logger.info(
            "[categories-debug] load_categories context | company_id=%s | manager_id=%s | region_id=%s | bypass=%s",
            self.company_id,
            self.manager_id,
            region_id,
            self.bypass_category_access,
        )

        if self.bypass_category_access:
            categories_raw = self.db.get_company_categories(self.company_id, region_id)
            logger.info(
                "[categories-debug] source=get_company_categories | raw_type=%s",
                type(categories_raw).__name__,
            )
        else:
            categories_raw = self.db.get_accessible_categories(
                self.company_id, self.manager_id, region_id
            )
            logger.info(
                "[categories-debug] source=get_accessible_categories | raw_type=%s",
                type(categories_raw).__name__,
            )
            with contextlib.suppress(Exception):
                self._visible_scope_node_ids = {
                    str(x)
                    for x in self.db.get_manager_scope_node_ids(
                        self.manager_id,
                        company_id=self.company_id,
                        permission="view",
                    )
                    if str(x).strip()
                }
        categories = categories_raw if isinstance(categories_raw, list) else []
        self._set_loading_text(f"Категорії: {len(categories)}")
        self.current_categories = categories
        logger.info("[categories-debug] categories fetched | count=%s", len(categories))

        if self.quick_filter == "missing_target_margin":
            filtered = []
            for category in categories:
                if not isinstance(category, dict):
                    continue
                raw_margin = category.get("target_margin_percent")
                if raw_margin is None or str(raw_margin).strip() == "":
                    margin = self.db.get_category_target_margin(category)
                else:
                    try:
                        margin = float(str(raw_margin).replace(",", "."))
                    except Exception:
                        margin = None
                if margin is None:
                    filtered.append(category)
            categories = filtered
            logger.info(
                "[categories-debug] quick_filter=missing_target_margin applied | count=%s",
                len(categories),
            )

        query = (
            (self.search_field.value or "").strip().lower()
            if hasattr(self, "search_field")
            else ""
        )
        logger.info("[categories-debug] query | value=%r", query)

        if not categories:
            logger.warning("[categories-debug] categories empty -> render empty state")
            self.categories_list.controls.append(
                ft.Container(
                    padding=ft.padding.only(left=4, top=6, bottom=6),
                    content=ft.Column(
                        [
                            ft.Text(
                                self.t("categories.empty_state", "Немає доступних категорій у вибраному контексті"),
                                color="grey600",
                                size=self.EMPTY_TEXT_SIZE,
                            ),
                            ft.Row(
                                [
                                    ft.OutlinedButton(
                                        self.t("categories.create_btn", "Створити категорію"),
                                        icon=ft.Icons.ADD,
                                        on_click=lambda _: self.show_category_dialog(),
                                        visible=(
                                            not self.superadmin_explicit_mode
                                            and self.can_manage_categories
                                            and not self.read_only_subordinate
                                        ),
                                    )
                                ]
                            ),
                        ],
                        spacing=8,
                        horizontal_alignment=ft.CrossAxisAlignment.START,
                    ),
                )
            )
            with contextlib.suppress(Exception):
                self.categories_list.controls.append(
                    self._build_category_archive_section(region_id)
                )
            with contextlib.suppress(Exception):
                self.categories_list.update()
            logger.info("[categories-debug] empty state rendered")
            return

        def _category_matches_query(category_row, text):
            if not text:
                return True
            if text in (category_row.get("name", "").lower()):
                return True

            cfg = self._category_levels_config(category_row)
            enabled_levels = [
                lvl
                for lvl in cfg.get("levels", [])
                if isinstance(lvl, dict) and lvl.get("enabled")
            ]
            if not enabled_levels:
                return False

            def _walk(level_row, parent_node_id=None):
                pos = int(level_row.get("position"))
                nodes = self.db.get_category_level_nodes(
                    category_row.get("id"), pos, parent_node_id=parent_node_id
                )
                next_level = self._next_enabled_level(enabled_levels, pos)
                for node in nodes:
                    if not isinstance(node, dict):
                        continue
                    if text in (node.get("name", "").lower()):
                        return True
                    if next_level and _walk(next_level, node.get("id")):
                        return True
                return False

            return _walk(enabled_levels[0])

        for category in categories:
            if not isinstance(category, dict):
                logger.warning(
                    "[categories-debug] skip non-dict category row | row_type=%s",
                    type(category).__name__,
                )
                continue
            if query and not _category_matches_query(category, query):
                logger.info(
                    "[categories-debug] category filtered out by query | id=%s | name=%s",
                    category.get("id"),
                    category.get("name"),
                )
                continue

            logger.info(
                "[categories-debug] rendering category card | id=%s | name=%s | region_id=%s",
                category.get("id"),
                category.get("name"),
                category.get("region_id"),
            )
            try:
                self.categories_list.controls.append(
                    self._create_category_card(category)
                )
            except Exception as ex:
                logger.exception(
                    "[categories-debug] render category failed | id=%s | name=%s | err=%s",
                    category.get("id"),
                    category.get("name"),
                    ex,
                )

        if not self.categories_list.controls:
            logger.warning(
                "[categories-debug] no controls after filter/render -> show no-results"
            )
            self.categories_list.controls.append(
                ft.Container(
                    content=ft.Text(
                        self.t("gsm.nothing_found", "Нічого не знайдено"),
                        color="grey600",
                        size=self.EMPTY_TEXT_SIZE,
                        text_align=ft.TextAlign.LEFT,
                    ),
                    padding=ft.padding.only(left=4, top=6, bottom=6),
                )
            )

        with contextlib.suppress(Exception):
            self.categories_list.controls.append(
                self._build_category_archive_section(region_id)
            )

        with contextlib.suppress(Exception):
            self.categories_list.update()
        logger.info(
            "[categories-debug] load_categories complete | rendered_controls=%s",
            len(self.categories_list.controls),
        )

    def _build_category_archive_section(self, region_id):
        """Згорнута секція архівованих категорій з можливістю відновлення."""
        archived_rows = (
            self.db.get_company_categories(
                self.company_id, region_id, manager_id=self.manager_id, archived=True
            )
            or []
        )
        archive_key = self._expanded_state_key()
        collapsed = CategoriesBrandsView._archive_collapsed_by_context.get(
            archive_key, True
        )

        def _toggle(_):
            CategoriesBrandsView._archive_collapsed_by_context[archive_key] = (
                not collapsed
            )
            self.load_categories()

        header = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(
                        ft.Icons.EXPAND_MORE if not collapsed else ft.Icons.CHEVRON_RIGHT,
                        size=18,
                        color="grey700",
                    ),
                    ft.Icon(ft.Icons.ARCHIVE_OUTLINED, size=16, color="grey700"),
                    ft.Text(
                        self.t("categories.archive_section", "Архів") + f" ({len(archived_rows)})",
                        size=13,
                        weight="bold",
                        color="grey700",
                    ),
                ],
                spacing=6,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=8),
            on_click=_toggle,
            ink=True,
        )
        controls = [header]
        if not collapsed:
            if not archived_rows:
                controls.append(
                    ft.Container(
                        padding=ft.padding.only(left=28, bottom=6),
                        content=ft.Text(
                            self.t("categories.archive_empty", "Архів порожній"),
                            color="grey600",
                            size=12,
                        ),
                    )
                )
            else:
                for row in archived_rows:
                    if not isinstance(row, dict):
                        continue
                    with contextlib.suppress(Exception):
                        controls.append(self._create_category_card(row, is_archived=True))
        return ft.Container(
            content=ft.Column(controls, spacing=2),
            margin=ft.margin.only(top=10),
            border=ft.border.only(top=ft.BorderSide(1, "grey300")),
        )

    def _brand_chip(self, brand, on_edit, on_delete, category=None):
        tm_type = brand.get("tm_type", "-")
        return ft.Container(
            padding=10,
            border=ft.border.all(1, _BORDER),
            border_radius=8,
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(
                                self._brand_label(brand, category),
                                weight="bold",
                                size=13,
                            ),
                            ft.Text(tm_type, size=11, color="grey700"),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.TextButton(
                        "Редагувати",
                        icon=ft.Icons.EDIT,
                        visible=not self.read_only_subordinate,
                        on_click=on_edit,
                    ),
                    ft.IconButton(
                        ft.Icons.ARCHIVE_OUTLINED,
                        icon_color="grey700",
                        tooltip="Архівувати",
                        visible=not self.read_only_subordinate,
                        on_click=on_delete,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
        )

    def _find_brand_row(self, category_id, brand_id):
        for row in self.db.get_brands(category_id):
            if isinstance(row, dict) and row.get("id") == brand_id:
                return row
        return None

    def _link_brand_to_series(self, category_id, brand_id, series_id):
        brand_row = self._find_brand_row(category_id, brand_id)
        if not isinstance(brand_row, dict):
            return False
        level_1_node_id, _ = self._extract_brand_parent_nodes(brand_row)
        return self.db.update_brand(
            brand_id,
            brand_row.get("name") or "",
            brand_row.get("tm_type") or self.tm_types[0],
            subcategory_id=level_1_node_id,
            series_id=series_id,
        )

    def _extract_brand_parent_nodes(self, brand_row):
        level_1_node_id = None
        level_2_node_id = None
        if not isinstance(brand_row, dict) or not brand_row.get("id"):
            return level_1_node_id, level_2_node_id
        try:
            for path_row in self.db.get_node_path(brand_row.get("id")):
                if not isinstance(path_row, dict):
                    continue
                pos = int(path_row.get("position", 0))
                if pos == 1 and not level_1_node_id:
                    level_1_node_id = path_row.get("id")
                elif pos == 2 and not level_2_node_id:
                    level_2_node_id = path_row.get("id")
        except Exception:
            logger.exception("Extract brand parent nodes failed")
        return level_1_node_id, level_2_node_id

    def _next_enabled_level(self, enabled_levels, current_position):
        for row in enabled_levels:
            if int(row.get("position")) > int(current_position):
                return row
        return None

    def show_level_node_dialog(
        self, category_id, level_position, level_title, parent_node_id=None, node=None
    ):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        is_edit = isinstance(node, dict)
        node = node or {}
        name_f = ft.TextField(
            label=f"Назва: {level_title}", value=node.get("name", ""), border_radius=10
        )

        async def _save(_):
            self._clear_field_errors(name_f)
            name_val = (name_f.value or "").strip()
            if not name_val:
                self._mark_required(name_f, self.t("categories.err.enter_name", "Введіть назву"))
                self._show_snack(self.t("categories.err.enter_name", "Введіть назву"), "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            if is_edit:
                ok = await asyncio.to_thread(
                    self.db.update_level_node, node.get("id"), name_val
                )
            else:
                created = await asyncio.to_thread(
                    lambda: self.db.find_or_create_level_node(
                        category_id,
                        level_position,
                        name_val,
                        parent_node_id=parent_node_id,
                    )
                )
                ok = bool(created)
                if ok and created and not self.bypass_category_access:
                    await asyncio.to_thread(
                        lambda nid=created: self.db.add_node_owner(
                            self.manager_id, self.company_id, nid
                        )
                    )

            if ok:
                self._close_dlg(dlg)
                self.load_categories()
                self._show_snack(self.t("orders.sync.synced", "Збережено"), "green")
            else:
                self._show_snack(self.t("categories.err.save", "Не вдалося зберегти"), "red")

        def _level_help(_):
            from views.help_utils import show_modal_help
            _pg = self._get_page_safe()
            show_modal_help(_pg, level_title, [
                self.t("categories.level_help.step1", "1. Введіть назву рівня ієрархії."),
                self.t("categories.level_help.step2", "2. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text(level_title, expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_level_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Container(
                width=420, content=ft.Column([name_f], tight=True, spacing=10)
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(dlg)),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=_save),
            ],
        )
        self._show_dialog_safe(dlg)

    def _archive_level_node_and_refresh(self, node_id):
        def confirm_archive(e):
            if self.db.archive_level_node(node_id):
                self._expanded_node_ids.discard(node_id)
                self._save_expanded_state()
                self._close_dlg(confirm_dlg)
                self.load_categories()
                self._show_snack("Рівень перенесено в архів", "blue")
            else:
                self._close_dlg(confirm_dlg)
                self._show_snack("Не вдалося архівувати рівень", "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(
                "Перенести рівень разом з усіма дочірніми рівнями в архів? Його можна буде відновити."
            ),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"),
                    on_click=lambda _: self._close_dlg(confirm_dlg),
                ),
                ft.TextButton("Архівувати", on_click=confirm_archive),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def _restore_level_node_and_refresh(self, node_id):
        if self.db.restore_level_node(node_id):
            self.load_categories()
            self._show_snack("Рівень відновлено з архіву", "blue")
        else:
            self._show_snack("Не вдалося відновити рівень", "red")

    def _delete_level_node_and_refresh(self, node_id):
        if self.read_only_subordinate or not self.can_manage_categories:
            self._show_snack(
                "Лише Адмін/СуперАдмін може керувати категоріями", "orange"
            )
            return

        def confirm_delete(e):
            ok, msg = self.db.delete_level_node(node_id)
            self._close_dlg(confirm_dlg)
            if ok:
                self._expanded_node_ids.discard(node_id)
                self._save_expanded_state()
                self.load_categories()
            self._show_snack(msg, "green" if ok else "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(
                "Видалити рівень разом з усіма дочірніми рівнями НАЗАВЖДИ? "
                "Це неможливо скасувати."
            ),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"),
                    on_click=lambda _: self._close_dlg(confirm_dlg),
                ),
                ft.TextButton(
                    "Видалити", on_click=confirm_delete, style=ft.ButtonStyle(color="red")
                ),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def _set_category_expanded(self, category_id, expanded):
        if not category_id:
            return
        if expanded:
            self._expanded_category_ids.add(category_id)
        else:
            self._expanded_category_ids.discard(category_id)
        self._save_expanded_state()

    def _on_category_expand_change(self, category_id, expanded):
        self._set_category_expanded(category_id, expanded)
        self.load_categories()

    def _set_node_expanded(self, node_id, expanded):
        if not node_id:
            return
        if expanded:
            self._expanded_node_ids.add(node_id)
        else:
            self._expanded_node_ids.discard(node_id)
        self._save_expanded_state()

    def _on_node_expand_change(self, node_id, expanded):
        self._set_node_expanded(node_id, expanded)
        self.load_categories()

    def _build_level_node_tile(self, category_id, levels, node, level_row, is_archived: bool = False):
        pos = int(level_row.get("position"))
        title = level_row.get("title") or f"Рівень {pos}"
        next_level = self._next_enabled_level(levels, pos)
        node_id = node.get("id")
        is_expanded = (not is_archived) and node_id in self._expanded_node_ids
        visible_scope = set(self._visible_scope_node_ids or set())
        add_next_button = None

        if next_level and not self.read_only_subordinate and not is_archived:
            add_next_button = ft.FilledButton(
                f"Створити {next_level.get('title') or f'Рівень {next_level.get('position')}'}",
                icon=ft.Icons.ADD,
                on_click=lambda _, p=node.get(
                    "id"
                ), nl=next_level: self.show_level_node_dialog(
                    category_id,
                    int(nl.get("position")),
                    nl.get("title") or f"Рівень {nl.get('position')}",
                    parent_node_id=p,
                ),
            )

        child_controls = []
        if next_level and is_expanded:
            child_nodes = self.db.get_category_level_nodes(
                category_id,
                int(next_level.get("position")),
                parent_node_id=node.get("id"),
            )
            if visible_scope:
                child_nodes = [
                    ch
                    for ch in child_nodes
                    if isinstance(ch, dict)
                    and str(ch.get("id") or "").strip() in visible_scope
                ]
            child_controls = [
                self._build_level_node_tile(category_id, levels, child, next_level)
                for child in child_nodes
                if isinstance(child, dict)
            ]
            with contextlib.suppress(Exception):
                archived_child_nodes = [
                    ch
                    for ch in (
                        self.db.get_category_level_nodes(
                            category_id,
                            int(next_level.get("position")),
                            parent_node_id=node.get("id"),
                            archived=True,
                        )
                        or []
                    )
                    if isinstance(ch, dict)
                ]
                if archived_child_nodes:
                    child_controls.append(
                        self._build_level_node_archive_section(
                            category_id,
                            levels,
                            next_level,
                            archived_child_nodes,
                            parent_key=f"{category_id}:{node.get('id')}",
                        )
                    )

        if add_next_button:
            child_controls = [add_next_button] + child_controls

        action_btns = []
        action_btns.append(
            ft.TextButton(
                "Редагувати",
                icon=ft.Icons.EDIT,
                visible=not self.read_only_subordinate and not is_archived,
                on_click=lambda _, n=node: self.show_level_node_dialog(
                    category_id,
                    pos,
                    title,
                    parent_node_id=node.get("parent_id"),
                    node=n,
                ),
            )
        )
        action_btns.append(
            ft.IconButton(
                ft.Icons.UNARCHIVE_OUTLINED if is_archived else ft.Icons.ARCHIVE_OUTLINED,
                icon_color="blue" if is_archived else "grey700",
                tooltip="Відновити з архіву" if is_archived else "Архівувати",
                visible=not self.read_only_subordinate,
                on_click=(
                    (lambda _, nid=node.get("id"): self._restore_level_node_and_refresh(nid))
                    if is_archived
                    else (lambda _, nid=node.get("id"): self._archive_level_node_and_refresh(nid))
                ),
            )
        )
        action_btns.append(
            ft.IconButton(
                ft.Icons.DELETE_OUTLINE,
                icon_color="red",
                tooltip="Видалити назавжди",
                visible=not self.read_only_subordinate and self.can_manage_categories,
                on_click=lambda _, nid=node.get("id"): self._delete_level_node_and_refresh(nid),
            )
        )

        if next_level:
            expand_toggle = (
                []
                if is_archived
                else [
                    ft.TextButton(
                        "Рівні ▾" if is_expanded else "Рівні ▸",
                        icon=ft.Icons.ACCOUNT_TREE,
                        on_click=lambda _, nid=node_id, expanded=is_expanded: self._on_node_expand_change(
                            nid, not expanded
                        ),
                    )
                ]
            )
            node_header = ft.Row(
                [
                    ft.Text(node.get("name", title), weight="bold", size=13),
                    ft.Row(
                        [*expand_toggle, *action_btns],
                        spacing=0,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

            body_controls = [node_header]
            if is_expanded:
                body_controls.extend(child_controls)
            else:
                body_controls.append(
                    ft.Text(
                        "Розгорніть рівень для завантаження дочірніх елементів",
                        size=11,
                        color="grey600",
                    )
                )

            return ft.Container(
                margin=ft.margin.only(top=6),
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                border=ft.border.all(1, _BORDER),
                border_radius=8,
                content=ft.Column(body_controls, spacing=8),
            )

        return ft.Container(
            margin=ft.margin.only(top=6),
            padding=ft.padding.symmetric(horizontal=10, vertical=8),
            border=ft.border.all(1, _BORDER),
            border_radius=8,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(node.get("name", title), size=13, weight="bold"),
                            ft.Row(action_btns, spacing=0),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    add_next_button if add_next_button else ft.Container(visible=False),
                ],
                spacing=8,
            ),
        )

    def _build_level_node_archive_section(
        self, category_id, levels, level_row, archived_nodes, parent_key
    ):
        """Згорнута секція архівованих дочірніх рівнів під конкретним вузлом ієрархії."""
        expanded_ids = CategoriesBrandsView._level_archive_expanded_by_context.setdefault(
            self._expanded_state_key(), set()
        )
        collapsed = parent_key not in expanded_ids

        def _toggle(_):
            if parent_key in expanded_ids:
                expanded_ids.discard(parent_key)
            else:
                expanded_ids.add(parent_key)
            self.load_categories()

        header = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(
                        ft.Icons.EXPAND_MORE if not collapsed else ft.Icons.CHEVRON_RIGHT,
                        size=16,
                        color="grey700",
                    ),
                    ft.Icon(ft.Icons.ARCHIVE_OUTLINED, size=14, color="grey700"),
                    ft.Text(
                        f"Архів ({len(archived_nodes)})",
                        size=12,
                        weight="bold",
                        color="grey700",
                    ),
                ],
                spacing=4,
            ),
            padding=ft.padding.symmetric(horizontal=6, vertical=6),
            on_click=_toggle,
            ink=True,
        )
        controls = [header]
        if not collapsed:
            for child in archived_nodes:
                if not isinstance(child, dict):
                    continue
                with contextlib.suppress(Exception):
                    controls.append(
                        self._build_level_node_tile(
                            category_id, levels, child, level_row, is_archived=True
                        )
                    )
        return ft.Container(
            content=ft.Column(controls, spacing=2),
            margin=ft.margin.only(top=4, left=4),
        )

    def _build_category_tree_controls(self, category, levels):
        category = category if isinstance(category, dict) else {}
        cat_id = category.get("id")
        if not cat_id or not levels:
            return []
        first = levels[0]
        first_pos = int(first.get("position"))
        top_nodes = self.db.get_category_level_nodes(
            cat_id, first_pos, parent_node_id=None
        )
        if self._visible_scope_node_ids:
            top_nodes = [
                node
                for node in top_nodes
                if isinstance(node, dict)
                and str(node.get("id") or "").strip() in self._visible_scope_node_ids
            ]
        controls = [
            self._build_level_node_tile(cat_id, levels, node, first)
            for node in top_nodes
            if isinstance(node, dict)
        ]
        with contextlib.suppress(Exception):
            archived_top_nodes = [
                node
                for node in (
                    self.db.get_category_level_nodes(
                        cat_id, first_pos, parent_node_id=None, archived=True
                    )
                    or []
                )
                if isinstance(node, dict)
            ]
            if archived_top_nodes:
                controls.append(
                    self._build_level_node_archive_section(
                        cat_id,
                        levels,
                        first,
                        archived_top_nodes,
                        parent_key=f"{cat_id}:root:{first_pos}",
                    )
                )
        return controls

    def _build_first_level_add_button(self, category, levels, text=None):
        category = category if isinstance(category, dict) else {}
        cat_id = category.get("id")
        if not cat_id or not levels or self.read_only_subordinate:
            return None
        first = levels[0]
        return ft.FilledButton(
            text
            or f"Створити {first.get('title') or f'Рівень {first.get('position')}'}",
            icon=ft.Icons.ADD,
            on_click=lambda _, lvl=first: self.show_level_node_dialog(
                cat_id,
                int(lvl.get("position")),
                lvl.get("title") or f"Рівень {lvl.get('position')}",
                parent_node_id=None,
            ),
        )

    def _create_category_card(self, category, is_archived: bool = False):
        cat_id = category.get("id")
        cfg = self._category_levels_config(category)
        levels = [
            lvl
            for lvl in cfg.get("levels", [])
            if isinstance(lvl, dict) and lvl.get("enabled")
        ]
        use_products = bool(cfg.get("use_products", True))
        is_expanded = (not is_archived) and cat_id in self._expanded_category_ids
        logger.info(
            "[categories-debug] _create_category_card | id=%s | name=%s | enabled_levels=%s | use_products=%s | expanded=%s",
            cat_id,
            category.get("name"),
            [int(level_row.get("position")) for level_row in levels],
            use_products,
            is_expanded,
        )

        deepest_title = (
            levels[-1].get("title") if levels else None
        ) or "останній рівень"
        mode_hint = (
            "Товари: окремий рівень" if use_products else f"Товар = {deepest_title}"
        )
        order_hint = "Ієрархія: " + (
            " -> ".join(
                (lvl.get("title") or f"Рівень {lvl.get('position')}") for lvl in levels
            )
            if levels
            else "(без активних рівнів)"
        )
        tree_controls = (
            self._build_category_tree_controls(category, levels) if is_expanded else []
        )

        first_add_button = self._build_first_level_add_button(category, levels)
        if not tree_controls:
            if levels and is_expanded:
                first_title = (
                    levels[0].get("title") or f"Рівень {levels[0].get('position')}"
                )
                tree_controls = [
                    ft.Text(
                        f"Немає записів у «{first_title}»", size=12, color="grey600"
                    )
                ]
            elif levels and not is_expanded:
                tree_controls = [
                    ft.Text(
                        "Розгорніть категорію для завантаження ієрархії",
                        size=12,
                        color="grey600",
                    )
                ]
            else:
                tree_controls = [
                    ft.Text(
                        "Рівні не налаштовано — натисніть Редагувати.",
                        size=12,
                        color="grey600",
                    )
                ]

        if first_add_button:
            tree_controls = [first_add_button] + tree_controls
        logger.info(
            "[categories-debug] _create_category_card using safe container renderer"
        )

        header_row = ft.Row(
            [
                ft.Container(
                    expand=True,
                    content=ft.Column(
                        [
                            ft.Text(
                                self._category_label(category),
                                weight="bold",
                                size=15,
                                selectable=True,
                            ),
                            ft.Text(mode_hint, size=11, color="grey700"),
                            ft.Text(order_hint, size=11, color="grey700"),
                        ],
                        spacing=2,
                    ),
                ),
                ft.TextButton(
                    "Ієрархія ▾" if is_expanded else "Ієрархія ▸",
                    icon=ft.Icons.ACCOUNT_TREE,
                    visible=not is_archived,
                    on_click=lambda _, cid=cat_id, expanded=is_expanded: self._on_category_expand_change(
                        cid, not expanded
                    ),
                ),
                ft.TextButton(
                    "Редагувати",
                    icon=ft.Icons.EDIT,
                    on_click=lambda _: self.show_category_dialog(category),
                    visible=not is_archived and not self.read_only_subordinate,
                ),
                ft.IconButton(
                    ft.Icons.UNARCHIVE_OUTLINED
                    if is_archived
                    else ft.Icons.ARCHIVE_OUTLINED,
                    icon_color="blue" if is_archived else "grey700",
                    tooltip=(
                        self.t("categories.restore", "Відновити з архіву")
                        if is_archived
                        else self.t("categories.archive", "Архівувати")
                    ),
                    on_click=(
                        (lambda _: self.restore_category(cat_id))
                        if is_archived
                        else (lambda _: self.archive_category(cat_id))
                    ),
                    visible=self.can_manage_categories
                    and not self.read_only_subordinate,
                ),
                ft.IconButton(
                    ft.Icons.DELETE_OUTLINE,
                    icon_color="red",
                    tooltip="Видалити назавжди",
                    on_click=lambda _, cid=cat_id: self.delete_category(cid),
                    visible=self.can_manage_categories
                    and not self.read_only_subordinate,
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.START,
            spacing=6,
        )

        body_controls = [header_row]
        if is_expanded:
            body_controls.extend(tree_controls)

        return ft.Row(
            [
                ft.Container(
                    expand=True,
                    margin=ft.margin.only(bottom=8),
                    padding=ft.padding.symmetric(horizontal=10, vertical=8),
                    border=ft.border.all(1, _BORDER2),
                    border_radius=10,
                    bgcolor=_PANEL,
                    content=ft.Column(body_controls, spacing=8),
                )
            ],
            expand=True,
        )

    def show_subcategory_dialog(self, category_id, subcategory=None):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        is_edit = isinstance(subcategory, dict)
        subcategory = subcategory or {}

        name_f = ft.TextField(
            label=self.t("categories.field.subcategory_name", "Назва підкатегорії"),
            value=subcategory.get("name", ""),
            border_radius=10,
        )
        quick_fill_block = ft.Container(visible=False)

        def apply_quick_fill(match):
            name_f.value = match.get("name", name_f.value)
            quick_fill_block.visible = False
            with contextlib.suppress(Exception):
                quick_fill_block.update()

        def on_search(e):
            query = (name_f.value or "").strip()

            def _run_search():
                if len(query) > 1 and not is_edit:
                    matches = self.db.search_subcategories_company(
                        self.company_id, query, 5
                    )
                    if matches:
                        controls = [ft.Text("Знайдено схожі підкатегорії:", size=12)]
                        for match in matches:
                            cat_info = (
                                match.get("categories", {})
                                if isinstance(match.get("categories"), dict)
                                else {}
                            )
                            controls.append(
                                ft.Row(
                                    [
                                        ft.Text(
                                            f"{self._subcategory_label(match, cat_info)} / {self._category_label(cat_info)}",
                                            size=12,
                                        ),
                                        ft.ElevatedButton(
                                            "Копіювати",
                                            icon=ft.Icons.COPY_ALL,
                                            on_click=lambda _, m=match: apply_quick_fill(
                                                m
                                            ),
                                        ),
                                    ],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                )
                            )
                        quick_fill_block.content = ft.Column(controls, spacing=5)
                        quick_fill_block.visible = True
                    else:
                        quick_fill_block.visible = False
                else:
                    quick_fill_block.visible = False

                with contextlib.suppress(Exception):
                    quick_fill_block.update()

            self._debounce_call("subcategory_dialog_search", _run_search)

        name_f.on_change = on_search

        def save_click(e):
            self._clear_field_errors(name_f)
            name_val = (name_f.value or "").strip()
            if not name_val:
                self._mark_required(name_f, self.t("categories.err.enter_name", "Введіть назву"))
                self._show_snack("Введіть назву підкатегорії", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            if self.db.subcategory_name_exists(
                category_id,
                name_val,
                exclude_subcategory_id=subcategory.get("id") if is_edit else None,
            ):
                self._mark_required(name_f, "Така підкатегорія вже існує")
                self._show_snack("Така підкатегорія вже існує", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            ok = (
                self.db.update_subcategory(subcategory.get("id"), name_val)
                if is_edit
                else self.db.add_subcategory(category_id, name_val)
            )
            if ok:
                self._close_dlg(dlg)
                self.load_categories()
                self._show_snack("Підкатегорію збережено", "green")
            else:
                self._show_snack("Помилка збереження підкатегорії", "red")

        def _subcat_help(_):
            from views.help_utils import show_modal_help
            _pg = self._get_page_safe()
            show_modal_help(_pg, self.t("categories.dlg.subcategory", "Підкатегорія"), [
                self.t("categories.subcat_help.step1", "1. Введіть назву підкатегорії."),
                self.t("categories.subcat_help.step2", "2. Або оберіть зі списку для швидкого заповнення."),
                self.t("categories.subcat_help.step3", "3. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text(self.t("categories.dlg.subcategory", "Підкатегорія"), expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_subcat_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Column(
                [name_f, quick_fill_block], tight=True, spacing=10, width=420
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(dlg)),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=save_click),
            ],
        )
        self._show_dialog_safe(dlg)

    def show_series_dialog(
        self, category_id, subcategory_id=None, series=None, brand_id=None
    ):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        is_edit = isinstance(series, dict)
        series = series or {}

        name_f = ft.TextField(
            label=self.t("categories.field.series_name", "Назва серії"), value=series.get("name", ""), border_radius=10
        )

        def save_click(e):
            self._clear_field_errors(name_f)
            name_val = (name_f.value or "").strip()
            if not name_val:
                self._mark_required(name_f, self.t("categories.err.enter_name", "Введіть назву"))
                self._show_snack("Введіть назву серії", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            if self.db.series_name_exists(
                category_id,
                name_val,
                subcategory_id,
                exclude_series_id=series.get("id") if is_edit else None,
            ):
                self._mark_required(name_f, "Така серія вже існує")
                self._show_snack("Така серія вже існує", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            ok = (
                self.db.update_series(series.get("id"), name_val)
                if is_edit
                else self.db.add_series(category_id, name_val, subcategory_id)
            )

            if ok and (not is_edit) and brand_id:
                created_series = None
                for row in self.db.get_series(category_id, subcategory_id):
                    if (
                        isinstance(row, dict)
                        and (row.get("name") or "").strip().lower() == name_val.lower()
                    ):
                        created_series = row
                if created_series and created_series.get("id"):
                    if not self._link_brand_to_series(
                        category_id, brand_id, created_series.get("id")
                    ):
                        self._show_snack(
                            "Серію створено, але не вдалося прив'язати до бренду",
                            "orange",
                        )

            if ok:
                self._close_dlg(dlg)
                self.load_categories()
                self._show_snack("Серію збережено", "green")
            else:
                self._show_snack("Помилка збереження серії", "red")

        def _series_dlg_help(_):
            from views.help_utils import show_modal_help
            _pg = self._get_page_safe()
            show_modal_help(_pg, self.t("categories.dlg.series", "Серія"), [
                self.t("categories.series_dlg_help.step1", "1. Введіть назву серії."),
                self.t("categories.series_dlg_help.step2", "2. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text(self.t("categories.dlg.series", "Серія"), expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_series_dlg_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Column([name_f], tight=True, spacing=10, width=420),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(dlg)),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=save_click),
            ],
        )
        self._show_dialog_safe(dlg)

    def show_category_dialog(self, category=None):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        is_edit = isinstance(category, dict)
        if (not is_edit) and (not self.can_manage_categories):
            self._show_snack(
                "Створювати категорії може лише Адмін/СуперАдмін", "orange"
            )
            return

        category = category or {}
        target_company_id = (
            self.company_id or category.get("company_id") or ""
        ).strip()
        edit_lock_token = ""
        lock_released_ref = {"done": False}
        lock_heartbeat_task_ref = {"task": None}
        if is_edit:
            lock_res = self.db.acquire_edit_lock(
                target_company_id,
                "categories",
                category.get("id"),
                self.manager_id,
                self.user_data.get("full_name") or "",
                ttl_seconds=180,
            )
            if not bool(lock_res.get("ok")):
                self._show_snack(
                    lock_res.get("message")
                    or "Категорія редагується іншим користувачем",
                    "orange",
                )
                return
            edit_lock_token = lock_res.get("lock_token") or ""

            async def _heartbeat_loop():
                while True:
                    await asyncio.sleep(90)
                    if lock_released_ref["done"]:
                        return
                    self.db.heartbeat_edit_lock(
                        target_company_id,
                        "categories",
                        category.get("id"),
                        self.manager_id,
                        edit_lock_token,
                        ttl_seconds=180,
                    )

            try:
                lock_heartbeat_task_ref["task"] = asyncio.create_task(_heartbeat_loop())
            except Exception:
                logger.exception("Failed to start category lock heartbeat")

        def _release_lock_once():
            if (not is_edit) or lock_released_ref["done"]:
                return
            lock_released_ref["done"] = True
            hb_task = lock_heartbeat_task_ref.get("task")
            if hb_task and not hb_task.done():
                hb_task.cancel()
            self.db.release_edit_lock(
                target_company_id,
                "categories",
                category.get("id"),
                self.manager_id,
                edit_lock_token,
            )

        can_rename_category = bool(self.can_manage_categories)
        name_f = ft.TextField(
            label=self.t("categories.field.category_name", "Назва категорії"),
            value=category.get("name", ""),
            border_radius=10,
            disabled=is_edit and not can_rename_category,
        )
        current_target_margin = None
        if isinstance(category, dict):
            try:
                raw_margin = category.get("target_margin_percent")
                if raw_margin is not None and str(raw_margin).strip() != "":
                    current_target_margin = float(str(raw_margin).replace(",", "."))
            except Exception:
                current_target_margin = None
        if current_target_margin is None:
            current_target_margin = self.db.get_category_target_margin(category)
        target_margin_f = ft.TextField(
            label=self.t("categories.field.target_margin", "Цільова рентабельність (%)"),
            value=(
                "" if current_target_margin is None else f"{current_target_margin:g}"
            ),
            border_radius=10,
            width=240,
            hint_text="Напр. 25",
        )

        def _wide_checkbox_row(label, value=False):
            cb = ft.Checkbox(value=bool(value))
            txt = ft.Text(label or "")

            def _toggle(_):
                cb.value = not bool(cb.value)
                with contextlib.suppress(Exception):
                    cb.update()

            row = ft.Container(
                content=ft.Row(
                    [cb, txt],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(horizontal=8, vertical=8),
                border_radius=8,
                ink=True,
                on_click=_toggle,
            )
            return cb, row

        if is_edit:
            levels_cfg = self._category_levels_config(category)
        else:
            region_id = self._resolve_region_id()
            company_cfg = self.db.get_company_catalog_config(self.company_id, region_id)
            levels_cfg = {
                "levels": company_cfg.get("levels")
                or self.db.get_category_levels_config(None).get("levels", []),
                "use_products": bool(company_cfg.get("use_products", False)),
            }
        levels_rows = []
        for row in levels_cfg.get("levels", []):
            try:
                pos = int(row.get("position", 0))
            except Exception:
                continue
            if pos < 1 or pos > 6:
                continue
            title_f = ft.TextField(
                label=f"Назва рівня {pos}",
                value=(row.get("title") or (f"Рівень {pos}" if not is_edit else "")),
                border_radius=10,
                width=370,
            )
            enabled_cb, enabled_row = _wide_checkbox_row(
                "Увімкнено", bool(row.get("enabled"))
            )
            levels_rows.append(
                {
                    "position": pos,
                    "title": title_f,
                    "enabled": enabled_cb,
                    "row": ft.Row([title_f, enabled_row], spacing=10),
                }
            )

        if not levels_rows:
            for pos in range(1, 7):
                title_f = ft.TextField(
                    label=f"Назва рівня {pos}",
                    value=f"Рівень {pos}",
                    border_radius=10,
                    width=370,
                )
                enabled_cb, enabled_row = _wide_checkbox_row("Увімкнено", pos <= 2)
                levels_rows.append(
                    {
                        "position": pos,
                        "title": title_f,
                        "enabled": enabled_cb,
                        "row": ft.Row([title_f, enabled_row], spacing=10),
                    }
                )

        # максимально простий елемент, щоб уникнути проблем з layout/expand у діалозі
        use_prod_cb = ft.Checkbox(
            label="Товари (окремий рівень)",
            value=bool(levels_cfg.get("use_products", True)),
        )
        use_prod_row = ft.Container(
            content=use_prod_cb,
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            border_radius=8,
        )

        levels_controls = [
            row.get("row")
            for row in levels_rows
            if isinstance(row, dict) and row.get("row") is not None
        ]
        if not levels_controls:
            levels_controls = [
                ft.Text(
                    "Рівні не знайдено. Буде застосована дефолтна конфігурація 1..6",
                    size=11,
                    color="grey700",
                )
            ]
        levels_controls.append(use_prod_row)

        def _current_hierarchy_levels():
            return [
                {
                    "position": row.get("position"),
                    "title": (
                        row.get("title").value or f"Рівень {row.get('position')}"
                    ).strip(),
                    "enabled": bool(row.get("enabled").value),
                }
                for row in levels_rows
            ]

        def save_click(e):
            self._clear_field_errors(name_f)
            name_val = (name_f.value or "").strip()
            if not name_val:
                self._mark_required(name_f, self.t("categories.err.enter_name", "Введіть назву"))
                self._show_snack("Введіть назву категорії", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            target_margin_val = None
            raw_target_margin = (target_margin_f.value or "").strip()
            if raw_target_margin:
                try:
                    target_margin_val = float(raw_target_margin.replace(",", "."))
                except Exception:
                    self._show_snack(
                        "Некоректне значення цільової рентабельності", "orange"
                    )
                    with contextlib.suppress(Exception):
                        name_f.update()
                    return

            hierarchy_config = {
                "use_products": bool(use_prod_cb.value),
                "levels": _current_hierarchy_levels(),
                "target_margin_percent": target_margin_val,
            }

            if is_edit:
                if can_rename_category and self.db.category_name_exists(
                    target_company_id,
                    self._resolve_region_id(),
                    name_val,
                    exclude_category_id=category.get("id"),
                ):
                    self._mark_required(name_f, "Така категорія вже існує")
                    self._show_snack("Така категорія вже існує", "orange")
                    with contextlib.suppress(Exception):
                        name_f.update()
                    return
                update_name = (
                    name_val
                    if can_rename_category
                    else (category.get("name") or name_val)
                )
                ok = self.db.update_category(
                    category.get("id"),
                    update_name,
                    hierarchy_config=hierarchy_config,
                    manager_id=self.manager_id,
                    lock_token=edit_lock_token,
                )
            else:
                region_id = self._resolve_region_id()
                if not region_id:
                    self._show_snack("Оберіть або налаштуйте регіон", "orange")
                    return
                if self.db.category_name_exists(self.company_id, region_id, name_val):
                    self._mark_required(name_f, "Така категорія вже існує")
                    self._show_snack("Така категорія вже існує", "orange")
                    with contextlib.suppress(Exception):
                        name_f.update()
                    return
                created_id = self.db.create_category_with_access(
                    self.company_id,
                    region_id,
                    name_val,
                    self.manager_id,
                    hierarchy_config=hierarchy_config,
                )
                ok = bool(created_id)

            if ok:
                _release_lock_once()
                self._close_dlg(dlg)
                self.load_categories()
                self._show_snack(self.t("categories.category_saved", "Категорію збережено"), "green")
            else:
                self._show_snack(self.t("categories.err.save_category", "Помилка збереження категорії"), "red")

        def _cancel(_):
            _release_lock_once()
            self._close_dlg(dlg)

        def _on_dismiss(_):
            _release_lock_once()

        def _cat_help(_):
            from views.help_utils import show_modal_help
            _pg = self._get_page_safe()
            show_modal_help(_pg, self.t("categories.dlg.help_title", "Категорія"), [
                self.t("categories.cat_help.step1", "1. Введіть назву категорії."),
                self.t("categories.cat_help.step2", "2. Вкажіть цільову рентабельність % (впливає на лічильник Головної)."),
                self.t("categories.cat_help.step3", "3. Налаштуйте рівні ієрархії (напр. Тип → Бренд → Серія → Товар)."),
                self.t("categories.cat_help.step4", "4. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text(self.t("categories.dlg.edit_title", "Редагувати категорію") if is_edit else self.t("categories.create_btn", "Створити категорію"), expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_cat_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            on_dismiss=_on_dismiss,
            content=ft.Column(
                [
                    name_f,
                    target_margin_f,
                    ft.Text("Конфігурація рівнів", size=12, weight="bold"),
                    *levels_controls,
                ],
                tight=True,
                spacing=10,
                width=580,
                scroll=ft.ScrollMode.ADAPTIVE,
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=_cancel),
                ft.ElevatedButton(self.t("common.save", "Зберегти"), on_click=save_click),
            ],
        )
        self._show_dialog_safe(dlg)

    def archive_category(self, category_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        if not self.can_manage_categories:
            self._show_snack(
                "Лише Адмін/СуперАдмін може керувати категоріями", "orange"
            )
            return

        def confirm_archive(e):
            if self.db.archive_category(category_id, manager_id=self.manager_id):
                self._expanded_category_ids.discard(category_id)
                self._save_expanded_state()
                self._close_dlg(confirm_dlg)
                self.load_categories()
                self._show_snack(self.t("categories.category_archived", "Категорію перенесено в архів"), "green")
            else:
                self._show_snack("Помилка архівації категорії", "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text("Перенести категорію разом з усією ієрархією рівнів в архів? Її можна буде відновити."),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(confirm_dlg)
                ),
                ft.TextButton("Архівувати", on_click=confirm_archive),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def restore_category(self, category_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return
        if not self.can_manage_categories:
            self._show_snack(
                "Лише Адмін/СуперАдмін може керувати категоріями", "orange"
            )
            return
        if self.db.restore_category(category_id):
            self.load_categories()
            self._show_snack(self.t("categories.category_restored", "Категорію відновлено з архіву"), "green")
        else:
            self._show_snack("Помилка відновлення категорії", "red")

    def delete_category(self, category_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return
        if not self.can_manage_categories:
            self._show_snack(
                "Лише Адмін/СуперАдмін може керувати категоріями", "orange"
            )
            return

        def confirm_delete(e):
            ok, msg = self.db.delete_category(self.company_id, category_id)
            self._close_dlg(confirm_dlg)
            if ok:
                self._expanded_category_ids.discard(category_id)
                self._save_expanded_state()
                self.load_categories()
                self._show_snack(msg, "green")
            else:
                self._show_snack(msg, "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text(
                "Видалити категорію разом з усією ієрархією рівнів НАЗАВЖДИ? "
                "Це неможливо скасувати. Якщо категорія використовується - "
                "видалення відхилиться, скористайтесь архівом."
            ),
            actions=[
                ft.TextButton(
                    self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(confirm_dlg)
                ),
                ft.TextButton(
                    "Видалити", on_click=confirm_delete, style=ft.ButtonStyle(color="red")
                ),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def show_brand_dialog(
        self, category_id, subcategory_id=None, series_id=None, brand=None
    ):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        is_edit = isinstance(brand, dict)
        brand = brand or {}

        name_f = ft.TextField(
            label=self.t("categories.field.brand_name", "Назва бренду"), value=(brand.get("name") or ""), border_radius=10
        )
        if is_edit:
            active_subcategory_id, active_series_id = self._extract_brand_parent_nodes(
                brand
            )
        else:
            active_subcategory_id = subcategory_id
            active_series_id = series_id

        series_f = ft.Dropdown(label="Серія (опційно)", border_radius=10, options=[])

        def load_series_options(sub_id):
            rows = self.db.get_series(category_id, sub_id)
            series_f.options = [ft.dropdown.Option(key="", text="Без серії")]
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    series_f.options.append(
                        ft.dropdown.Option(
                            key=row.get("id"), text=self._series_label(row)
                        )
                    )
            candidate = active_series_id or ""
            available = {
                opt.key
                for opt in series_f.options
                if getattr(opt, "key", None) is not None
            }
            series_f.value = candidate if candidate in available else ""

        load_series_options(active_subcategory_id)

        tm_type_f = ft.Dropdown(
            label="Тип ТМ",
            options=[ft.dropdown.Option(x) for x in self.tm_types],
            value=(brand.get("tm_type") or (self.tm_types[0] if not is_edit else "")),
            border_radius=10,
        )
        quick_fill_block = ft.Container(visible=False)

        def apply_quick_fill(match):
            name_f.value = match.get("name", name_f.value)
            tm_type_f.value = match.get("tm_type", tm_type_f.value)
            quick_fill_block.visible = False
            with contextlib.suppress(Exception):
                name_f.update()
            with contextlib.suppress(Exception):
                tm_type_f.update()
            with contextlib.suppress(Exception):
                quick_fill_block.update()

        def on_search(e):
            query = (name_f.value or "").strip()

            def _run_search():
                if len(query) > 1 and not is_edit:
                    matches = self.db.search_brands_company(self.company_id, query, 5)
                    if matches:
                        controls = [ft.Text("Знайдено схожі бренди:", size=12)]
                        for match in matches:
                            cat_info = (
                                match.get("categories", {})
                                if isinstance(match.get("categories"), dict)
                                else {}
                            )
                            controls.append(
                                ft.Row(
                                    [
                                        ft.Text(
                                            f"{self._brand_label(match, cat_info)} / {self._category_label(cat_info)}",
                                            size=12,
                                        ),
                                        ft.ElevatedButton(
                                            "Копіювати",
                                            icon=ft.Icons.COPY_ALL,
                                            on_click=lambda _, m=match: apply_quick_fill(
                                                m
                                            ),
                                        ),
                                    ],
                                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                )
                            )
                        quick_fill_block.content = ft.Column(controls, spacing=5)
                        quick_fill_block.visible = True
                    else:
                        quick_fill_block.visible = False
                else:
                    quick_fill_block.visible = False

                with contextlib.suppress(Exception):
                    quick_fill_block.update()

            self._debounce_call("brand_dialog_search", _run_search)

        name_f.on_change = on_search

        def save_click(e):
            self._clear_field_errors(name_f)
            name_val = (name_f.value or "").strip()
            if not name_val:
                self._mark_required(name_f, self.t("categories.err.enter_name", "Введіть назву"))
                self._show_snack("Введіть назву бренду", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            tm_type_val = tm_type_f.value or self.tm_types[0]
            selected_series = series_f.value or None
            if self.db.brand_name_exists(
                category_id,
                name_val,
                active_subcategory_id,
                selected_series,
                exclude_brand_id=brand.get("id") if is_edit else None,
            ):
                self._mark_required(name_f, "Такий бренд вже існує")
                self._show_snack("Такий бренд вже існує", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return
            if is_edit:
                ok = self.db.update_brand(
                    brand.get("id"),
                    name_val,
                    tm_type_val,
                    subcategory_id=active_subcategory_id,
                    series_id=selected_series,
                )
            else:
                ok = self.db.add_brand(
                    category_id,
                    name_val,
                    tm_type_val,
                    active_subcategory_id,
                    selected_series,
                )
            if ok:
                self._close_dlg(dlg)
                self.load_categories()
                self._show_snack(self.t("categories.brand_saved", "Бренд збережено"), "green")
            else:
                self._show_snack(self.t("categories.err.save_brand", "Помилка збереження бренду"), "red")

        def _brand_edit_help(_):
            from views.help_utils import show_modal_help
            _pg = self._get_page_safe()
            show_modal_help(_pg, "Бренд", [
                self.t("categories.brand_help.step1", "1. Введіть назву бренду."),
                self.t("categories.brand_help.step2", "2. Оберіть серію (якщо є)."),
                self.t("categories.brand_help.step3", "3. Вкажіть тип ТМ (торгова марка, ліцензія тощо)."),
                self.t("categories.brand_help.step4", "4. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text("Бренд", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_brand_edit_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Column(
                [name_f, series_f, tm_type_f, quick_fill_block],
                tight=True,
                spacing=10,
                width=450,
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(dlg)),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=save_click),
            ],
        )
        self._show_dialog_safe(dlg)

    def archive_subcategory(self, subcategory_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        def confirm_archive(e):
            if self.db.archive_subcategory(subcategory_id):
                self._close_dlg(confirm_dlg)
                self.load_categories()
                self._show_snack(self.t("categories.subcategory_archived", "Підкатегорію перенесено в архів"), "blue")
            else:
                self._show_snack("Не вдалося архівувати підкатегорію", "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text("Перенести підкатегорію в архів?"),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(self.t("common.yes", "Так"), on_click=confirm_archive),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def restore_subcategory(self, subcategory_id):
        if self.db.restore_subcategory(subcategory_id):
            self.load_categories()
            self._show_snack(self.t("categories.subcategory_restored", "Підкатегорію відновлено з архіву"), "blue")
        else:
            self._show_snack("Не вдалося відновити підкатегорію", "red")

    def delete_subcategory(self, subcategory_id):
        if self.read_only_subordinate or not self.can_manage_categories:
            self._show_snack(
                "Лише Адмін/СуперАдмін може керувати категоріями", "orange"
            )
            return

        def confirm_delete(e):
            ok, msg = self.db.delete_subcategory(self.company_id, subcategory_id)
            self._close_dlg(confirm_dlg)
            if ok:
                self.load_categories()
            self._show_snack(msg, "green" if ok else "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text("Видалити підкатегорію НАЗАВЖДИ? Це неможливо скасувати."),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(
                    self.t("common.yes", "Так"), on_click=confirm_delete, style=ft.ButtonStyle(color="red")
                ),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def archive_series(self, series_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        def confirm_archive(e):
            if self.db.archive_series(series_id):
                self._close_dlg(confirm_dlg)
                self.load_categories()
                self._show_snack(self.t("categories.series_archived", "Серію перенесено в архів"), "blue")
            else:
                self._show_snack("Не вдалося архівувати серію", "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text("Перенести серію в архів?"),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(self.t("common.yes", "Так"), on_click=confirm_archive),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def restore_series(self, series_id):
        if self.db.restore_series(series_id):
            self.load_categories()
            self._show_snack(self.t("categories.series_restored", "Серію відновлено з архіву"), "blue")
        else:
            self._show_snack("Не вдалося відновити серію", "red")

    def delete_series(self, series_id):
        if self.read_only_subordinate or not self.can_manage_categories:
            self._show_snack(
                "Лише Адмін/СуперАдмін може керувати категоріями", "orange"
            )
            return

        def confirm_delete(e):
            ok, msg = self.db.delete_series(self.company_id, series_id)
            self._close_dlg(confirm_dlg)
            if ok:
                self.load_categories()
            self._show_snack(msg, "green" if ok else "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text("Видалити серію НАЗАВЖДИ? Це неможливо скасувати."),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(
                    self.t("common.yes", "Так"), on_click=confirm_delete, style=ft.ButtonStyle(color="red")
                ),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def show_quick_subcategory_dialog(self):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        if not self.current_categories:
            self._show_snack("Спочатку створіть категорію", "orange")
            return

        category_options = [
            ft.dropdown.Option(key=c.get("id"), text=self._category_label(c))
            for c in self.current_categories
            if isinstance(c, dict) and c.get("id")
        ]
        if not category_options:
            self._show_snack("Немає категорій для вибору", "orange")
            return

        first_category_id = next(
            (
                c.get("id")
                for c in self.current_categories
                if isinstance(c, dict) and c.get("id")
            ),
            None,
        )
        category_f = ft.Dropdown(
            label="Категорія",
            border_radius=10,
            options=category_options,
            value=first_category_id,
        )
        name_f = ft.TextField(label="Назва підгрупи", border_radius=10)

        def save_click(_):
            self._clear_field_errors(category_f, name_f)
            category_id = category_f.value
            name_val = (name_f.value or "").strip()
            if not category_id or not name_val:
                if not category_id:
                    self._mark_required(category_f, "Оберіть категорію")
                if not name_val:
                    self._mark_required(name_f, self.t("categories.err.enter_name", "Введіть назву"))
                self._show_snack("Оберіть категорію та введіть назву", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return
            if self.db.subcategory_name_exists(category_id, name_val):
                self._mark_required(name_f, "Така підгрупа вже існує")
                self._show_snack("Така підгрупа вже існує", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return
            if self.db.add_subcategory(category_id, name_val):
                self._close_dlg(dlg)
                self.load_categories()
                self._show_snack("Підгрупу створено", "green")
            else:
                self._show_snack("Не вдалося створити підгрупу", "red")

        def _subgroup_help(_):
            from views.help_utils import show_modal_help
            _pg = self._get_page_safe()
            show_modal_help(_pg, "Нова підгрупа", [
                self.t("categories.subgroup_help.step1", "1. Оберіть категорію."),
                self.t("categories.subgroup_help.step2", "2. Введіть назву підгрупи."),
                self.t("categories.subgroup_help.step3", "3. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text("Нова підгрупа", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_subgroup_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Column([category_f, name_f], tight=True, spacing=10, width=420),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(dlg)),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=save_click),
            ],
        )
        self._show_dialog_safe(dlg)

    def show_quick_brand_dialog(self):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        if not self.current_categories:
            self._show_snack("Спочатку створіть категорію", "orange")
            return

        category_options = [
            ft.dropdown.Option(key=c.get("id"), text=self._category_label(c))
            for c in self.current_categories
            if isinstance(c, dict) and c.get("id")
        ]
        if not category_options:
            self._show_snack("Немає категорій для вибору", "orange")
            return

        first_category_id = next(
            (
                c.get("id")
                for c in self.current_categories
                if isinstance(c, dict) and c.get("id")
            ),
            None,
        )
        category_f = ft.Dropdown(
            label="Категорія",
            border_radius=10,
            options=category_options,
            value=first_category_id,
        )
        subcategory_f = ft.Dropdown(
            label="Підгрупа (опційно)", border_radius=10, options=[]
        )
        series_f = ft.Dropdown(label="Серія (опційно)", border_radius=10, options=[])
        name_f = ft.TextField(label=self.t("categories.field.brand_name", "Назва бренду"), value="No_Name", border_radius=10)
        tm_type_f = ft.Dropdown(
            label="Тип ТМ",
            border_radius=10,
            options=[ft.dropdown.Option(x) for x in self.tm_types],
            value=self.tm_types[0],
        )

        def refresh_series(_=None):
            selected_category_id = category_f.value
            selected_sub_id = subcategory_f.value or None
            rows = (
                self.db.get_series(selected_category_id, selected_sub_id)
                if selected_category_id
                else []
            )
            series_f.options = [ft.dropdown.Option(key="", text="Без серії")]
            for row in rows:
                if isinstance(row, dict) and row.get("id"):
                    series_f.options.append(
                        ft.dropdown.Option(
                            key=row.get("id"), text=self._series_label(row)
                        )
                    )
            series_f.value = ""
            with contextlib.suppress(Exception):
                series_f.update()

        def refresh_subcategories(_=None):
            selected_category_id = category_f.value
            subs = (
                self.db.get_subcategories(selected_category_id)
                if selected_category_id
                else []
            )
            subcategory_f.options = [ft.dropdown.Option(key="", text="Без підгрупи")]
            selected_category = next(
                (
                    c
                    for c in self.current_categories
                    if isinstance(c, dict) and c.get("id") == selected_category_id
                ),
                None,
            )
            for s in subs:
                if isinstance(s, dict) and s.get("id"):
                    subcategory_f.options.append(
                        ft.dropdown.Option(
                            key=s.get("id"),
                            text=self._subcategory_label(s, selected_category),
                        )
                    )
            subcategory_f.value = ""
            refresh_series()
            with contextlib.suppress(Exception):
                subcategory_f.update()

        category_f.on_select = refresh_subcategories
        subcategory_f.on_select = refresh_series
        refresh_subcategories()

        def save_click(_):
            self._clear_field_errors(category_f, name_f)
            category_id = category_f.value
            name_val = (name_f.value or "").strip()
            tm_type_val = tm_type_f.value or self.tm_types[0]
            sub_id = subcategory_f.value or None
            ser_id = series_f.value or None

            if not category_id or not name_val:
                if not category_id:
                    self._mark_required(category_f, "Оберіть категорію")
                if not name_val:
                    self._mark_required(name_f, self.t("categories.err.enter_name", "Введіть назву"))
                self._show_snack("Оберіть категорію та введіть назву бренду", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            if self.db.brand_name_exists(category_id, name_val, sub_id, ser_id):
                self._mark_required(name_f, "Такий бренд вже існує")
                self._show_snack("Такий бренд вже існує", "orange")
                with contextlib.suppress(Exception):
                    name_f.update()
                return

            if self.db.add_brand(category_id, name_val, tm_type_val, sub_id, ser_id):
                self._close_dlg(dlg)
                self.load_categories()
                self._show_snack("Бренд створено", "green")
            else:
                self._show_snack("Не вдалося створити бренд", "red")

        def _new_brand_help(_):
            from views.help_utils import show_modal_help
            _pg = self._get_page_safe()
            show_modal_help(_pg, "Новий бренд", [
                self.t("categories.new_brand_help.step1", "1. Оберіть категорію і підкатегорію."),
                self.t("categories.new_brand_help.step2", "2. Оберіть серію (якщо є)."),
                self.t("categories.new_brand_help.step3", "3. Введіть назву бренду."),
                self.t("categories.new_brand_help.step4", "4. Вкажіть тип ТМ."),
                self.t("categories.new_brand_help.step5", "5. Натисніть «Зберегти»."),
            ])

        dlg = ft.AlertDialog(
            title=ft.Row(
                [
                    ft.Text("Новий бренд", expand=True),
                    ft.IconButton(icon=ft.Icons.HELP_OUTLINE, icon_size=16, tooltip=self.t("common.help.tooltip", "Довідка"), on_click=_new_brand_help),
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER, tight=True,
            ),
            content=ft.Column(
                [category_f, subcategory_f, series_f, name_f, tm_type_f],
                tight=True,
                spacing=10,
                width=450,
            ),
            actions=[
                ft.TextButton(self.t("common.cancel", "Скасувати"), on_click=lambda _: self._close_dlg(dlg)),
                ft.FilledButton(self.t("common.save", "Зберегти"), on_click=save_click),
            ],
        )
        self._show_dialog_safe(dlg)

    def archive_brand(self, brand_id):
        if self.read_only_subordinate:
            self._show_snack(self.t("packaging.subordinate_readonly", "У вкладці підлеглого доступний лише перегляд"), "orange")
            return

        def confirm_archive(e):
            if self.db.archive_brand(brand_id):
                self._close_dlg(confirm_dlg)
                self.load_categories()
                self._show_snack(self.t("categories.brand_archived", "Бренд перенесено в архів"), "blue")
            else:
                self._show_snack("Не вдалося архівувати бренд", "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text("Перенести бренд в архів?"),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(self.t("common.yes", "Так"), on_click=confirm_archive),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def restore_brand(self, brand_id):
        if self.db.restore_brand(brand_id):
            self.load_categories()
            self._show_snack(self.t("categories.brand_restored", "Бренд відновлено з архіву"), "blue")
        else:
            self._show_snack("Не вдалося відновити бренд", "red")

    def delete_brand(self, brand_id):
        if self.read_only_subordinate or not self.can_manage_categories:
            self._show_snack(
                "Лише Адмін/СуперАдмін може керувати категоріями", "orange"
            )
            return

        def confirm_delete(e):
            ok, msg = self.db.delete_brand(self.company_id, brand_id)
            self._close_dlg(confirm_dlg)
            if ok:
                self.load_categories()
            self._show_snack(msg, "green" if ok else "red")

        confirm_dlg = ft.AlertDialog(
            title=ft.Text(self.t("common.confirm", "Підтвердження")),
            content=ft.Text("Видалити бренд НАЗАВЖДИ? Це неможливо скасувати."),
            actions=[
                ft.TextButton(self.t("common.no", "Ні"), on_click=lambda _: self._close_dlg(confirm_dlg)),
                ft.TextButton(
                    self.t("common.yes", "Так"), on_click=confirm_delete, style=ft.ButtonStyle(color="red")
                ),
            ],
        )
        self._show_dialog_safe(confirm_dlg)

    def _close_dlg(self, dlg):
        page = self._get_page_safe()
        if not page:
            logger.warning("[modal:categories] close skipped: page is None")
            return
        logger.info(
            "[modal:categories] close:start dlg=%s@%s open=%s overlay_before=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )
        page.pop_dialog()
        logger.info(
            "[modal:categories] close:finish dlg=%s@%s open=%s overlay_after=%s",
            type(dlg).__name__,
            id(dlg),
            getattr(dlg, "open", None),
            len(getattr(page, "overlay", []) or []),
        )

    def _show_snack(self, text, color):
        page = self._get_page_safe()
        if page:
            page.snack_bar = ft.SnackBar(
                ft.Text(text, color=ft.Colors.WHITE, weight="bold"),
                bgcolor=color,
                duration=3000,
            )
            page.snack_bar.open = True
            page.update()

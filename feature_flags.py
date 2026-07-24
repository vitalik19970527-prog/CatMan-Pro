"""Single source of truth for toggleable tabs/features.

Always-on tabs (Категорії, Товари, Постачальники, Замовлення) are
intentionally NOT listed here — they have no toggle and no key.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class FeatureDef:
    key: str
    label_uk: str
    requires: tuple[str, ...] = field(default_factory=tuple)


TOGGLEABLE_FEATURES: dict[str, FeatureDef] = {
    "packaging": FeatureDef("packaging", "Пакування"),
    "files": FeatureDef("files", "Файли"),
    "payments": FeatureDef("payments", "Взаєморозрахунки"),
    "cost_price_orders": FeatureDef("cost_price_orders", "С/В Замовлення"),
    "cost_price_stock": FeatureDef("cost_price_stock", "С/В Склад"),
    "pack_stock": FeatureDef(
        "pack_stock", "Склад Пакування", requires=("packaging",)
    ),
    "media_plan": FeatureDef("media_plan", "Медіа План"),
    "schedule": FeatureDef("schedule", "Розклад Замовлень"),
    "calculators": FeatureDef("calculators", "Калькулятори"),
    "notebook": FeatureDef("notebook", "Блокнот"),
}

FEATURE_ORDER: list[str] = list(TOGGLEABLE_FEATURES.keys())

# Roles that implicitly have full access and are excluded from per-user
# feature-override lists (mirrors views/extensions_view.py's _SA_ROLES).
ADMIN_ROLES = {"admin", "админ", "адмін", "superadmin", "суперадмин", "суперадмін"}


def transitive_requires(key: str) -> set[str]:
    """All features (direct + indirect) that `key` depends on."""
    seen: set[str] = set()
    stack = [key]
    while stack:
        cur = stack.pop()
        feat = TOGGLEABLE_FEATURES.get(cur)
        if not feat:
            continue
        for req in feat.requires:
            if req not in seen:
                seen.add(req)
                stack.append(req)
    return seen


def is_role_admin_like(role: str) -> bool:
    norm = (role or "").strip().lower().replace(" ", "")
    return norm in ADMIN_ROLES

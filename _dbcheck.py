from database import DataManager
db = DataManager()

# Тест напряму через метод
try:
    result = db.get_gsm_merged_results(limit=10)
    print("get_gsm_merged_results повернув:", len(result), "записів")
    if result:
        print("Перший запис:", list(result[0].keys()))
except Exception as e:
    import traceback
    print("Помилка в get_gsm_merged_results:", e)
    traceback.print_exc()

# Тест без suppress — знаходимо справжню помилку
print("\n--- Без suppress ---")
try:
    col_map = {
        "brand": "g.brand",
        "device_type": "g.device_type",
        "gsm_name": "g.model_name",
        "last_seen_at": "g.last_seen_at",
        "year": "g.specs->>'year'",
    }
    order_col = "g.brand"
    direction = "ASC"
    params = [2000]
    query = f"""
        SELECT
            g.id, g.brand, g.device_type,
            g.model_name AS gsm_name,
            g.image_url,
            g.source_url AS gsm_url,
            g.specs AS gsm_specs,
            g.last_seen_at,
            e.id AS ek_id,
            e.model_name AS ek_name,
            e.source_url AS ek_url,
            e.specs AS ek_specs
        FROM public.gsm_parser_devices g
        LEFT JOIN public.gsm_parser_devices e
            ON e.source = 'E-Katalog'
            AND (e.specs->>'gsm_ref_device_id') = g.id
        WHERE g.source = 'GSM Arena'
        ORDER BY {order_col} {direction}
        LIMIT %s
    """
    with db._pg().cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall() or []
        print(f"Результат: {len(rows)} рядків")
        if rows:
            first = rows[0]
            print("Тип першого рядка:", type(first))
            print("isinstance dict?", isinstance(first, dict))
            print("Перший:", first)
except Exception as e:
    import traceback
    print("Помилка:", e)
    traceback.print_exc()

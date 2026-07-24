-- Додаткові зовнішні ключі після базових таблиць (001).
-- manager_workspace_state створюється в 001 раніше за managers — тут додаємо зв'язок.

BEGIN;

ALTER TABLE manager_workspace_state
  DROP CONSTRAINT IF EXISTS fk_manager_workspace_state_manager;
ALTER TABLE manager_workspace_state
  ADD CONSTRAINT fk_manager_workspace_state_manager
  FOREIGN KEY (manager_id) REFERENCES managers (id) ON DELETE CASCADE;

-- Опційно: цімність даних (потрібно, щоб region_id/… посилались на реальні id, а не на '')
-- Виконуйте тільки якщо очищені «порожні» рядки в посиланнях.
-- ALTER TABLE categories ADD CONSTRAINT fk_categories_region
--   FOREIGN KEY (region_id) REFERENCES regions (id) ON DELETE CASCADE;
-- ALTER TABLE suppliers ADD CONSTRAINT fk_suppliers_region
--   FOREIGN KEY (region_id) REFERENCES regions (id) ON DELETE SET NULL;
-- ALTER TABLE level_nodes ADD CONSTRAINT fk_level_nodes_parent
--   FOREIGN KEY (parent_node_id) REFERENCES level_nodes (id) ON DELETE SET NULL;

COMMIT;

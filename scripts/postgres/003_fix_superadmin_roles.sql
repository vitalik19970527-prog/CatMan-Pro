-- Виправлення ролі для входу /auth/check: бекенд шукає супер-адміна з role = 'superadmin'
-- (у застосунку при реєстрації могло зберегтися «СуперАдмін»).
-- Виконайте після підключення до своєї БД (один раз).

BEGIN;

UPDATE managers
SET role = 'superadmin'
WHERE lower(replace(btrim(role), ' ', '')) IN ('суперадмін', 'суперадмин', 'superadmin')
  AND (role IS DISTINCT FROM 'superadmin');

COMMIT;

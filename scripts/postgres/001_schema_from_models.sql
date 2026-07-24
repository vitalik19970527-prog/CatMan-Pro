-- CatMan Pro — схема PostgreSQL, згенерована з SQLAlchemy (backend.app.models).
--   python scripts/postgres/generate_schema.py
--
-- Приклад:
--   createdb -U postgres catman
--   psql -U postgres -d catman -f scripts/postgres/001_schema_from_models.sql

BEGIN;


CREATE TABLE companies (
	id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE edit_locks (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	scope VARCHAR(64) NOT NULL, 
	entity_id VARCHAR NOT NULL, 
	manager_id VARCHAR NOT NULL, 
	token VARCHAR(128) NOT NULL, 
	expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE files (
	id VARCHAR NOT NULL, 
	bucket VARCHAR(64) NOT NULL, 
	filename VARCHAR(255) NOT NULL, 
	content_type VARCHAR(128) NOT NULL, 
	size INTEGER NOT NULL, 
	path TEXT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE manager_workspace_state (
	manager_id VARCHAR NOT NULL, 
	state JSONB NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (manager_id)
)

;

CREATE TABLE sync_pulses (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	scope VARCHAR(64) NOT NULL, 
	ts INTEGER NOT NULL, 
	changed_by VARCHAR NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE catalog_config (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	region_id VARCHAR NOT NULL, 
	config JSONB NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE categories (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	region_id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE delivery_methods (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	region_id VARCHAR NOT NULL, 
	methods JSONB NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE managers (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	full_name VARCHAR(255) NOT NULL, 
	email VARCHAR(255) NOT NULL, 
	phone VARCHAR(64) NOT NULL, 
	role VARCHAR(64) NOT NULL, 
	password_hash TEXT NOT NULL, 
	is_approved BOOLEAN NOT NULL, 
	meta JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE orders (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	region_id VARCHAR NOT NULL, 
	manager_id VARCHAR NOT NULL, 
	supplier_id VARCHAR NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE packaging (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	region_id VARCHAR NOT NULL, 
	manager_id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE products (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	region_id VARCHAR NOT NULL, 
	manager_id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE regions (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE supplier_settlements (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	region_id VARCHAR NOT NULL, 
	supplier_id VARCHAR NOT NULL, 
	currency VARCHAR(16) NOT NULL, 
	type VARCHAR(64) NOT NULL, 
	amount VARCHAR(64) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE suppliers (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	region_id VARCHAR NOT NULL, 
	manager_id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
)

;

CREATE TABLE brands (
	id VARCHAR NOT NULL, 
	category_id VARCHAR NOT NULL, 
	subcategory_id VARCHAR NOT NULL, 
	series_id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	tm_type VARCHAR(64) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(category_id) REFERENCES categories (id) ON DELETE CASCADE
)

;

CREATE TABLE level_nodes (
	id VARCHAR NOT NULL, 
	category_id VARCHAR NOT NULL, 
	position INTEGER NOT NULL, 
	parent_node_id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(category_id) REFERENCES categories (id) ON DELETE CASCADE
)

;

CREATE TABLE region_price_tiers (
	id VARCHAR NOT NULL, 
	company_id VARCHAR NOT NULL, 
	region_id VARCHAR NOT NULL, 
	tiers JSONB NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE, 
	FOREIGN KEY(region_id) REFERENCES regions (id) ON DELETE CASCADE
)

;

CREATE TABLE series (
	id VARCHAR NOT NULL, 
	category_id VARCHAR NOT NULL, 
	subcategory_id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(category_id) REFERENCES categories (id) ON DELETE CASCADE
)

;

CREATE TABLE subcategories (
	id VARCHAR NOT NULL, 
	category_id VARCHAR NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	data JSONB NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(category_id) REFERENCES categories (id) ON DELETE CASCADE
)

;

CREATE INDEX ix_companies_name ON companies (name);
CREATE INDEX ix_edit_locks_entity_id ON edit_locks (entity_id);
CREATE INDEX ix_edit_locks_manager_id ON edit_locks (manager_id);
CREATE INDEX ix_edit_locks_company_id ON edit_locks (company_id);
CREATE INDEX ix_edit_locks_scope ON edit_locks (scope);
CREATE INDEX ix_edit_locks_token ON edit_locks (token);
CREATE INDEX ix_files_bucket ON files (bucket);
CREATE INDEX ix_sync_pulses_ts ON sync_pulses (ts);
CREATE INDEX ix_sync_pulses_company_id ON sync_pulses (company_id);
CREATE INDEX ix_sync_pulses_scope ON sync_pulses (scope);
CREATE INDEX ix_catalog_config_region_id ON catalog_config (region_id);
CREATE INDEX ix_catalog_config_company_id ON catalog_config (company_id);
CREATE INDEX ix_categories_region_id ON categories (region_id);
CREATE INDEX ix_categories_company_id ON categories (company_id);
CREATE INDEX ix_categories_name ON categories (name);
CREATE INDEX ix_delivery_methods_region_id ON delivery_methods (region_id);
CREATE INDEX ix_delivery_methods_company_id ON delivery_methods (company_id);
CREATE INDEX ix_managers_phone ON managers (phone);
CREATE INDEX ix_managers_company_id ON managers (company_id);
CREATE INDEX ix_managers_email ON managers (email);
CREATE INDEX ix_orders_manager_id ON orders (manager_id);
CREATE INDEX ix_orders_region_id ON orders (region_id);
CREATE INDEX ix_orders_supplier_id ON orders (supplier_id);
CREATE INDEX ix_orders_company_id ON orders (company_id);
CREATE INDEX ix_packaging_manager_id ON packaging (manager_id);
CREATE INDEX ix_packaging_name ON packaging (name);
CREATE INDEX ix_packaging_region_id ON packaging (region_id);
CREATE INDEX ix_packaging_company_id ON packaging (company_id);
CREATE INDEX ix_products_region_id ON products (region_id);
CREATE INDEX ix_products_name ON products (name);
CREATE INDEX ix_products_company_id ON products (company_id);
CREATE INDEX ix_products_manager_id ON products (manager_id);
CREATE INDEX ix_regions_company_id ON regions (company_id);
CREATE INDEX ix_supplier_settlements_region_id ON supplier_settlements (region_id);
CREATE INDEX ix_supplier_settlements_company_id ON supplier_settlements (company_id);
CREATE INDEX ix_supplier_settlements_supplier_id ON supplier_settlements (supplier_id);
CREATE INDEX ix_suppliers_manager_id ON suppliers (manager_id);
CREATE INDEX ix_suppliers_region_id ON suppliers (region_id);
CREATE INDEX ix_suppliers_name ON suppliers (name);
CREATE INDEX ix_suppliers_company_id ON suppliers (company_id);
CREATE INDEX ix_brands_subcategory_id ON brands (subcategory_id);
CREATE INDEX ix_brands_name ON brands (name);
CREATE INDEX ix_brands_category_id ON brands (category_id);
CREATE INDEX ix_brands_series_id ON brands (series_id);
CREATE INDEX ix_level_nodes_category_id ON level_nodes (category_id);
CREATE INDEX ix_level_nodes_parent_node_id ON level_nodes (parent_node_id);
CREATE INDEX ix_level_nodes_position ON level_nodes (position);
CREATE INDEX ix_level_nodes_name ON level_nodes (name);
CREATE INDEX ix_region_price_tiers_region_id ON region_price_tiers (region_id);
CREATE INDEX ix_region_price_tiers_company_id ON region_price_tiers (company_id);
CREATE INDEX ix_series_category_id ON series (category_id);
CREATE INDEX ix_series_name ON series (name);
CREATE INDEX ix_series_subcategory_id ON series (subcategory_id);
CREATE INDEX ix_subcategories_category_id ON subcategories (category_id);
CREATE INDEX ix_subcategories_name ON subcategories (name);


-- Опційно (після очищення '' у посиланнях): зв'язок workspace з managers
-- ALTER TABLE manager_workspace_state
--   ADD CONSTRAINT fk_manager_workspace_state_manager
--   FOREIGN KEY (manager_id) REFERENCES managers(id) ON DELETE CASCADE;

COMMIT;

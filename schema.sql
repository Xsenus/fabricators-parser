CREATE SCHEMA IF NOT EXISTS "registry_fabricators";

CREATE TABLE registry_fabricators.companies (
	id UUID NOT NULL,
	source_url TEXT NOT NULL,
	source_node_id TEXT,
	name TEXT NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	full_name TEXT,
	inn_raw TEXT,
	inn_normalized VARCHAR(12),
	inn_is_valid BOOLEAN NOT NULL,
	ogrn VARCHAR(15),
	kpp VARCHAR(9),
	description TEXT,
	address TEXT,
	legal_address TEXT,
	site_url TEXT,
	domain TEXT,
	registration_date DATE,
	employee_count INTEGER,
	capital NUMERIC(24, 2),
	source_updated_at DATE,
	phone_status TEXT,
	email_status TEXT,
	record_status TEXT NOT NULL,
	metadata_jsonb JSONB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_url)
);

CREATE INDEX ix_registry_fabricators_companies_inn_normalized ON registry_fabricators.companies (inn_normalized);

CREATE TABLE registry_fabricators.crawl_queue (
	url TEXT NOT NULL,
	kind TEXT NOT NULL,
	owner_url TEXT,
	status TEXT NOT NULL,
	attempts INTEGER NOT NULL,
	priority INTEGER NOT NULL,
	available_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_error TEXT,
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (url)
);

CREATE TABLE registry_fabricators.crawl_runs (
	id UUID NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	status TEXT NOT NULL,
	requests INTEGER NOT NULL,
	processed INTEGER NOT NULL,
	access_reference TEXT,
	summary_jsonb JSONB,
	PRIMARY KEY (id)
);

CREATE TABLE registry_fabricators.enterprise_nodes (
	id UUID NOT NULL,
	source_url TEXT NOT NULL,
	source_node_id TEXT,
	name TEXT NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	metadata_jsonb JSONB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_url)
);

CREATE TABLE registry_fabricators.goods_categories (
	id UUID NOT NULL,
	source_url TEXT NOT NULL,
	source_node_id TEXT,
	name TEXT NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	metadata_jsonb JSONB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_url)
);

CREATE TABLE registry_fabricators.issues (
	id UUID NOT NULL,
	source_url TEXT NOT NULL,
	code TEXT NOT NULL,
	detail TEXT,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id)
);

CREATE TABLE registry_fabricators.page_snapshots (
	id UUID NOT NULL,
	source_url TEXT NOT NULL,
	kind TEXT NOT NULL,
	fetched_at TIMESTAMP WITH TIME ZONE NOT NULL,
	content_hash VARCHAR(64) NOT NULL,
	parser_version TEXT NOT NULL,
	payload_jsonb JSONB NOT NULL,
	PRIMARY KEY (id)
);

CREATE INDEX ix_registry_fabricators_page_snapshots_source_url ON registry_fabricators.page_snapshots (source_url);

CREATE TABLE registry_fabricators.product_types (
	id UUID NOT NULL,
	source_url TEXT NOT NULL,
	source_node_id TEXT,
	name TEXT NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	metadata_jsonb JSONB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_url)
);

CREATE TABLE registry_fabricators.schema_versions (
	version SERIAL NOT NULL,
	installed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (version)
);

CREATE TABLE registry_fabricators.company_enterprises (
	company_id UUID NOT NULL,
	enterprise_id UUID NOT NULL,
	source_url TEXT NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (company_id, enterprise_id),
	FOREIGN KEY(company_id) REFERENCES registry_fabricators.companies (id),
	FOREIGN KEY(enterprise_id) REFERENCES registry_fabricators.enterprise_nodes (id)
);

CREATE INDEX ix_registry_fabricators_company_enterprises_enterprise_id ON registry_fabricators.company_enterprises (enterprise_id);

CREATE TABLE registry_fabricators.company_product_types (
	company_id UUID NOT NULL,
	product_type_id UUID NOT NULL,
	source_url TEXT NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (company_id, product_type_id),
	FOREIGN KEY(company_id) REFERENCES registry_fabricators.companies (id),
	FOREIGN KEY(product_type_id) REFERENCES registry_fabricators.product_types (id)
);

CREATE INDEX ix_registry_fabricators_company_product_types_product_type_id ON registry_fabricators.company_product_types (product_type_id);

CREATE TABLE registry_fabricators.company_sites (
	id UUID NOT NULL,
	company_id UUID NOT NULL,
	site_url TEXT NOT NULL,
	domain TEXT,
	label TEXT,
	source_url TEXT NOT NULL,
	validation_status TEXT NOT NULL,
	is_primary BOOLEAN NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	is_current BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (company_id, site_url),
	FOREIGN KEY(company_id) REFERENCES registry_fabricators.companies (id)
);

CREATE INDEX ix_registry_fabricators_company_sites_company_id ON registry_fabricators.company_sites (company_id);

CREATE TABLE registry_fabricators.contacts (
	id UUID NOT NULL,
	company_id UUID NOT NULL,
	contact_type VARCHAR(16) NOT NULL,
	raw_value TEXT NOT NULL,
	normalized_value TEXT NOT NULL,
	source_url TEXT NOT NULL,
	extraction_method TEXT NOT NULL,
	validation_status TEXT NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	is_current BOOLEAN NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (company_id, contact_type, normalized_value),
	FOREIGN KEY(company_id) REFERENCES registry_fabricators.companies (id)
);

CREATE INDEX ix_registry_fabricators_contacts_company_id ON registry_fabricators.contacts (company_id);

CREATE TABLE registry_fabricators.enterprise_edges (
	parent_id UUID NOT NULL,
	child_id UUID NOT NULL,
	source_url TEXT NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (parent_id, child_id),
	FOREIGN KEY(parent_id) REFERENCES registry_fabricators.enterprise_nodes (id),
	FOREIGN KEY(child_id) REFERENCES registry_fabricators.enterprise_nodes (id)
);

CREATE INDEX ix_registry_fabricators_enterprise_edges_child_id ON registry_fabricators.enterprise_edges (child_id);

CREATE TABLE registry_fabricators.product_type_edges (
	parent_id UUID NOT NULL,
	child_id UUID NOT NULL,
	source_url TEXT NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (parent_id, child_id),
	FOREIGN KEY(parent_id) REFERENCES registry_fabricators.product_types (id),
	FOREIGN KEY(child_id) REFERENCES registry_fabricators.product_types (id)
);

CREATE INDEX ix_registry_fabricators_product_type_edges_child_id ON registry_fabricators.product_type_edges (child_id);

CREATE TABLE registry_fabricators.products (
	id UUID NOT NULL,
	source_url TEXT NOT NULL,
	source_node_id TEXT,
	name TEXT NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE NOT NULL,
	company_id UUID NOT NULL,
	description TEXT,
	price_text TEXT,
	price NUMERIC(24, 2),
	currency VARCHAR(8),
	source_updated_at DATE,
	record_status TEXT NOT NULL,
	metadata_jsonb JSONB NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (source_url),
	FOREIGN KEY(company_id) REFERENCES registry_fabricators.companies (id)
);

CREATE INDEX ix_registry_fabricators_products_company_id ON registry_fabricators.products (company_id);

CREATE TABLE registry_fabricators.product_category_links (
	product_id UUID NOT NULL,
	category_id UUID NOT NULL,
	source_url TEXT NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (product_id, category_id),
	FOREIGN KEY(product_id) REFERENCES registry_fabricators.products (id),
	FOREIGN KEY(category_id) REFERENCES registry_fabricators.goods_categories (id)
);

CREATE INDEX ix_registry_fabricators_product_category_links_category_id ON registry_fabricators.product_category_links (category_id);

CREATE TABLE registry_fabricators.product_type_links (
	product_id UUID NOT NULL,
	product_type_id UUID NOT NULL,
	source_url TEXT NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (product_id, product_type_id),
	FOREIGN KEY(product_id) REFERENCES registry_fabricators.products (id),
	FOREIGN KEY(product_type_id) REFERENCES registry_fabricators.product_types (id)
);

CREATE INDEX ix_registry_fabricators_product_type_links_product_type_id ON registry_fabricators.product_type_links (product_type_id);

INSERT INTO registry_fabricators.schema_versions(version, installed_at) VALUES (1, CURRENT_TIMESTAMP);

-- Run in the target DB as its administrator, after restoring schema + data.
-- Consumer is a role with no login; grant it to the existing application's DB role.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'registry_fabricators_reader') THEN
    CREATE ROLE registry_fabricators_reader NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA registry_fabricators TO registry_fabricators_reader;
GRANT SELECT ON
  registry_fabricators.companies,
  registry_fabricators.enterprise_nodes,
  registry_fabricators.product_types,
  registry_fabricators.goods_categories,
  registry_fabricators.company_enterprises,
  registry_fabricators.company_product_types,
  registry_fabricators.enterprise_edges,
  registry_fabricators.product_type_edges,
  registry_fabricators.products,
  registry_fabricators.product_type_links,
  registry_fabricators.product_category_links,
  registry_fabricators.contacts
TO registry_fabricators_reader;
-- Application administrator runs with the real existing role name:
-- GRANT registry_fabricators_reader TO your_existing_application_db_role;
-- No grants to PostgreSQL PUBLIC. No write grants. No tenant_id or tenant RLS.
-- Operational tables/snapshots/issues are not exposed by this role.

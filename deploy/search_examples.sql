-- $1 is a bound SQL parameter: '%' || user_text || '%'; never concatenate raw SQL.
-- Companies declaring a selected product type.
SELECT c.id, c.name, c.inn_normalized, c.address, c.site_url, pt.id AS product_type_id, pt.name AS product_type
FROM registry_fabricators.companies AS c
JOIN registry_fabricators.company_product_types AS cp ON cp.company_id = c.id
JOIN registry_fabricators.product_types AS pt ON pt.id = cp.product_type_id
WHERE pt.name ILIKE $1 AND c.record_status = 'parsed';

-- Goods with the producing company, independent from product types.
SELECT p.id, p.name, p.description, p.price_text, p.price, p.currency, c.id AS company_id, c.name AS company_name
FROM registry_fabricators.products AS p
JOIN registry_fabricators.companies AS c ON c.id = p.company_id
WHERE p.name ILIKE $1 AND p.record_status = 'parsed';

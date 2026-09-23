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

-- Direct product types declared on one company card (many-to-many).
SELECT c.name AS company_name, pt.name AS product_type, pt.source_url AS product_type_url
FROM registry_fabricators.companies AS c
JOIN registry_fabricators.company_product_types AS cp ON cp.company_id = c.id
JOIN registry_fabricators.product_types AS pt ON pt.id = cp.product_type_id
WHERE c.source_url = 'https://fabricators.ru/proizvoditel/kzmi-snab'
ORDER BY pt.name;

-- Direct enterprise types declared on one company card (many-to-many).
SELECT c.name AS company_name, en.name AS enterprise_type, en.source_url AS enterprise_url
FROM registry_fabricators.companies AS c
JOIN registry_fabricators.company_enterprises AS ce ON ce.company_id = c.id
JOIN registry_fabricators.enterprise_nodes AS en ON en.id = ce.enterprise_id
WHERE c.source_url = 'https://fabricators.ru/proizvoditel/kzmi-snab'
ORDER BY en.name;

import dataclasses
import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import httpx
import sqlalchemy as sa
import fabricators_parser as f

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'
COMPANY = f.BASE + '/proizvoditel/nizhegorodsike-avtokomponenty'
PRODUCT = f.BASE + '/tovar/stalnye-shtampovannye-kolyosa-marki-gaz'
TYPE = f.BASE + '/produkt/pressy'


def fixture(name, url):
    return f.parse_html((FIXTURES / name).read_text('utf-8'), url)


class ExtractionTests(unittest.TestCase):
    def test_interruptible_wait_clamps_clock_race(self):
        with patch.object(f.time, 'monotonic', side_effect=[0.0, 0.0, 1.0, 1.0]), \
             patch.object(f.time, 'sleep') as sleep:
            f.interruptible_wait(0.5)
        sleep.assert_called_once_with(0)

    def test_company_real_markup(self):
        r = fixture('company.html', COMPANY)
        self.assertEqual(r['source_node_id'], '84028')
        self.assertEqual(len(r['product_types']), 9)
        self.assertEqual(len(r['enterprise_nodes']), 2)
        self.assertEqual([p['url'] for p in r['products']], [PRODUCT])
        self.assertEqual(r['fields']['inn_normalized'], '5256083213')
        self.assertEqual(r['fields']['phone_status'], 'masked')
        self.assertEqual(r['fields']['email_status'], 'masked')
        self.assertEqual(r['contacts'], [])
        self.assertEqual(r['fields']['domain'], 'nautocom.ru')

    def test_product_real_markup_excludes_recommendations(self):
        r = fixture('product.html', PRODUCT)
        self.assertEqual(r['source_node_id'], '84029')
        self.assertEqual(r['owner_url'], COMPANY)
        self.assertEqual(str(r['fields']['price']), '1.00')
        self.assertEqual(r['fields']['currency'], 'RUB')
        self.assertIn('102.3101015-01', r['fields']['description'])
        self.assertEqual(r['metadata']['price_qualifier'], 'from')
        self.assertEqual(len(r['metadata']['goods_categories']), 3)
        self.assertEqual(r['product_types'], [])
        self.assertTrue(all('/tovary/' in x['url'] for x in r['metadata']['goods_categories']))

    def test_product_owner_conflict(self):
        with self.assertRaises(f.ParseError):
            f.parse_html((FIXTURES / 'product.html').read_text(encoding='utf-8'), PRODUCT,
                         owner_hint=f.BASE + '/proizvoditel/another')

    def test_type_id_and_pagination(self):
        r = fixture('type.html', TYPE)
        self.assertEqual(r['name'], 'Прессы')
        self.assertEqual(r['source_node_id'], '25768')
        self.assertEqual(len(r['ancestors']), 2)
        self.assertTrue(any('?page=1' in x['url'] for x in r['discovered']))
        self.assertFalse('products' in r)

    def test_contacts_full_and_partial(self):
        s = f.BeautifulSoup('<div class="field_email"><div class="line"><a href="mailto:Info@example.org">Info@example.org</a></div><div class="line">xxxxx@example.org</div></div>', 'html.parser')
        vals, status = f.collect_contacts(s, 'email')
        self.assertEqual(status, 'partial')
        self.assertEqual(vals[0]['normalized'], 'info@example.org')
        self.assertIsNone(f.normalize_contact('email', 'xxxx@example.org'))
        self.assertIsNone(f.normalize_contact('phone', '+7910058X-XX'))
        self.assertEqual(f.normalize_contact('phone', '8 (800) 123-45-67 доб. 12'), '+78001234567')

    def test_inn_checksum_and_invalid_input(self):
        self.assertTrue(f.inn_valid('5256083213'))
        self.assertFalse(f.inn_valid('5256083214'))
        self.assertFalse(f.inn_valid('tax-number'))

    def test_challenge_and_invalid_layout(self):
        self.assertTrue(f.looks_blocked('<title>Just a moment...</title>'))
        with self.assertRaises(f.ParseError):
            f.parse_html('<h1>Ошибка</h1>', COMPANY)

    def test_url_domain_query_and_rubric(self):
        self.assertEqual(f.canonical(f.BASE + '/produkt/'), f.BASE + '/sitemap/produkt')
        self.assertIsNone(f.canonical('https://evil.example/proizvoditel/test'))
        self.assertIsNone(f.canonical(f.BASE + '/produkt/pressy?sort=abc', keep_query=True))
        self.assertEqual(f.canonical(TYPE + '?page=02#top', keep_query=True), TYPE + '?page=2')

    def test_xml_sitemaps(self):
        xml = '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://fabricators.ru/sitemap.xml?page=1</loc></sitemap></sitemapindex>'
        self.assertEqual(f.parse_sitemap(xml), [f.BASE + '/sitemap.xml?page=1'])
        with self.assertRaises(f.ParseError):
            f.parse_sitemap('<!DOCTYPE a><urlset/>')


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db = f.Database('sqlite://')
        self.db.init()

    def save(self, record):
        with self.db.engine.begin() as c:
            self.db.save(c, record)

    def test_relations_idempotency_and_goods_owner(self):
        self.save(fixture('company.html', COMPANY))
        self.save(fixture('product.html', PRODUCT))
        self.save(fixture('type.html', TYPE))
        before = f.status(self.db)['tables']
        self.save(fixture('company.html', COMPANY))
        self.save(fixture('product.html', PRODUCT))
        self.assertEqual(f.status(self.db)['tables'], before)
        self.assertEqual(before['companies'], 1)
        self.assertEqual(before['products'], 1)
        self.assertEqual(before['company_product_types'], 9)
        self.assertEqual(before['product_category_links'], 3)
        self.assertEqual(before['product_type_links'], 0)
        with self.db.engine.connect() as c:
            p = c.execute(sa.select(f.products)).mappings().one()
            self.assertEqual(p['company_id'], f.uid('companies', COMPANY))

    def test_missing_relation_does_not_erase_existing(self):
        self.save(fixture('company.html', COMPANY))
        r = fixture('company.html', COMPANY)
        r['present'].remove('product_types')
        r['product_types'] = []
        self.save(r)
        self.assertEqual(f.status(self.db)['tables']['company_product_types'], 9)
        r['present'].append('product_types')
        self.save(r)
        self.assertEqual(f.status(self.db)['tables']['company_product_types'], 0)

    def test_owner_not_changed_by_other_catalog(self):
        self.save(fixture('company.html', COMPANY))
        r = fixture('company.html', COMPANY)
        r['source_url'] = f.BASE + '/proizvoditel/other'
        self.save(r)
        with self.db.engine.connect() as c:
            self.assertEqual(c.execute(sa.select(f.products.c.company_id)).scalar(), f.uid('companies', COMPANY))
        self.assertEqual(f.status(self.db)['tables']['issues'], 1)

    def test_old_contacts_preserved_when_masked(self):
        r = fixture('company.html', COMPANY)
        r['fields']['email_status'] = 'public'
        r['contacts'] = [{'kind': 'email', 'raw': 'a@example.org', 'normalized': 'a@example.org', 'method': 'html'}]
        self.save(r)
        self.save(fixture('company.html', COMPANY))
        with self.db.engine.connect() as c:
            self.assertTrue(c.execute(sa.select(f.contacts.c.is_current)).scalar())

    def test_shared_schema_and_pg_types(self):
        sql = f.schema_sql()
        self.assertIn('JSONB', sql)
        self.assertIn('UUID', sql)
        self.assertIn('registry_fabricators', sql)
        self.assertNotIn('tenant_id', sql)
        self.assertTrue(all(t.schema == f.SCHEMA for t in f.M.tables.values()))

    def test_export_import_roundtrip_and_refuse_merge(self):
        self.save(fixture('company.html', COMPANY))
        self.save(fixture('product.html', PRODUCT))
        self.save(fixture('type.html', TYPE))
        with tempfile.TemporaryDirectory() as tmp:
            f.export_bundle(self.db, tmp)
            other = f.Database('sqlite://')
            other.init()
            f.import_bundle(other, tmp)
            self.assertEqual(f.status(self.db)['tables'], f.status(other)['tables'])
            with self.assertRaises(ValueError):
                f.import_bundle(other, tmp)
            with pathlib.Path(tmp, 'companies.jsonl').open('a') as out:
                out.write('{}\n')
            with self.assertRaises(ValueError):
                f.import_bundle(other, tmp)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = f.Config(data_dir=self.temp.name, min_delay=1, max_delay=1)
        self.wait = patch.object(f, 'interruptible_wait')
        self.wait.start()
        self.addCleanup(self.wait.stop)

    def network(self, handler):
        n = f.Network(self.cfg, transport=httpx.MockTransport(handler))
        self.addCleanup(n.close)
        return n

    def test_robots_and_no_request_for_denied_url(self):
        calls = []
        def handler(r):
            calls.append(str(r.url))
            return httpx.Response(200, text='User-agent: *\nDisallow: /zavody?\nAllow: /proizvoditel/\n')
        n = self.network(handler)
        n.load_robots()
        with self.assertRaises(f.RobotsDenied):
            n.get(f.BASE + '/zavody?page=1')
        self.assertEqual(len(calls), 1)
        self.assertTrue(n.permits(COMPANY))

    def test_429_pause_persisted_on_restart(self):
        n = self.network(lambda r: httpx.Response(429, headers={'Retry-After': '120'}))
        n.robots = f.Protego.parse('User-agent: *\nAllow: /')
        with self.assertRaises(f.StopRun):
            n.get(COMPANY)
        reloaded = f.RateLimiter(self.cfg)
        with self.assertRaises(f.StopRun):
            reloaded.before()

    def test_external_redirect_and_403_stop(self):
        n = self.network(lambda r: httpx.Response(302, headers={'Location': 'https://example.org/'}))
        n.robots = f.Protego.parse('User-agent: *\nAllow: /')
        with self.assertRaises(f.StopRun):
            n.get(COMPANY)
        with self.assertRaises(f.StopRun):
            n.status(403, {})

    def test_size_budget_and_retry(self):
        n = self.network(lambda r: httpx.Response(200, headers={'Content-Type': 'text/html'}, text='a' * 50))
        n.cfg.max_bytes = 20
        n.robots = f.Protego.parse('User-agent: *\nAllow: /')
        with self.assertRaises(f.ParseError):
            n.get(COMPANY)
        with self.assertRaises(f.RetryPage):
            n.status(503, {})
        self.assertEqual(f.retry_after('120'), 120)

    def test_crawler_mocked_chain_and_resume(self):
        pages = {COMPANY: (FIXTURES / 'company.html').read_text(encoding='utf-8'), PRODUCT: (FIXTURES / 'product.html').read_text(encoding='utf-8')}
        def handler(request):
            path = str(request.url)
            if path.endswith('/robots.txt'):
                return httpx.Response(200, text='User-agent: *\nAllow: /')
            return httpx.Response(200, headers={'Content-Type': 'text/html'}, text=pages[path])
        self.cfg.access_reference = 'test-fixture-agreement'
        self.cfg.browser_contacts = False
        db = f.Database('sqlite://')
        db.init()
        with db.engine.begin() as c:
            db.enqueue(c, COMPANY)
        real_class = f.Network
        def make(cfg):
            return real_class(cfg, transport=httpx.MockTransport(handler))
        with patch.object(f, 'Network', side_effect=make):
            result = f.crawl(db, self.cfg, 2)
            self.assertEqual(result['processed'], 2)
        self.assertEqual(f.status(db)['tables']['products'], 1)
        with db.engine.connect() as c:
            self.assertEqual(c.execute(sa.select(f.products.c.record_status)).scalar(), 'parsed')
        self.assertEqual(f.status(db)['queue']['done'], 2)


if __name__ == '__main__':
    unittest.main()

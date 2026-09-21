"""Optional real PostgreSQL test. Use ONLY an empty disposable test database."""
import os
import tempfile
import unittest
import sqlalchemy as sa
import fabricators_parser as f
from test_parser import fixture, COMPANY, PRODUCT, TYPE


@unittest.skipUnless(os.environ.get('FABRICATORS_TEST_DATABASE_URL'), 'Disposable PostgreSQL test DSN not supplied')
class PostgreSQLTests(unittest.TestCase):
    def test_real_postgres_roundtrip(self):
        db = f.Database(os.environ['FABRICATORS_TEST_DATABASE_URL'])
        self.assertEqual(db.engine.dialect.name, 'postgresql')
        with db.engine.connect() as c:
            exists = c.execute(sa.text('SELECT 1 FROM pg_namespace WHERE nspname=:schema'), {'schema': f.SCHEMA}).scalar()
        self.assertFalse(exists, 'Refusing to touch a database with an existing registry schema')
        db.init()
        # The test deliberately leaves its schema for inspection; it never drops data.
        for name, url in [('company.html', COMPANY), ('product.html', PRODUCT), ('type.html', TYPE)]:
            with db.engine.begin() as c:
                db.save(c, fixture(name, url))
                db.save(c, fixture(name, url))
        with tempfile.TemporaryDirectory() as tmp:
            manifest = f.export_bundle(db, tmp)
            self.assertTrue(manifest['tables'])
        self.assertEqual(f.status(db)['tables']['products'], 1)
        with f.exclusive(db, f.Config(data_dir=tempfile.mkdtemp())):
            self.assertEqual(f.status(db)['tables']['company_product_types'], 9)

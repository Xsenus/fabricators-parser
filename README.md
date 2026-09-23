# Fabricators Parser

Автономный возобновляемый сборщик публичного каталога предприятий
[Fabricators.ru](https://fabricators.ru/). Сохраняет компании, товарные
направления, товары, контакты и исходные снимки в отдельную схему PostgreSQL
`registry_fabricators`.

## Возможности

- последовательный обход с ограничением частоты и поддержкой `robots.txt`;
- контрольные точки и безопасное возобновление после остановки;
- сохранение исходных записей без потери дополнительных полей;
- сохранение всех телефонов, e-mail и сайтов карточки, включая несколько
  значений внутри одной отображаемой строки;
- идемпотентная загрузка и история изменений;
- экспорт данных и схемы для переноса;
- офлайн-тесты на сохранённых HTML-фикстурах.

Результат исправления данных от 23.09.2026 и пример запросов для связей
«компания ↔ тип продукции/предприятия» описаны в [DATASET_STATUS_RU.md](DATASET_STATUS_RU.md).

## Быстрый старт

Требуются Python 3.11+ и PostgreSQL 14+.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
export DATABASE_URL='postgresql://user:password@127.0.0.1/registry'
.venv/bin/python fabricators_parser.py --config config.json init
.venv/bin/python fabricators_parser.py --config config.json seed
.venv/bin/python fabricators_parser.py --config config.json crawl
```

Статус и экспорт:

```bash
.venv/bin/python fabricators_parser.py --config config.json status
.venv/bin/python fabricators_parser.py --config config.json export ./exports
```

## Тесты

```bash
python -m unittest discover -s tests -v
```

Интеграционные тесты PostgreSQL запускаются только с отдельной тестовой БД,
указанной в `FABRICATORS_TEST_DATABASE_URL`.

## Безопасность и ограничения

Не коммитьте `.env`, браузерное состояние, дампы и собранные данные. Перед
массовым запуском проверьте условия использования источника, `robots.txt` и
задайте идентифицируемый `User-Agent`. Сбор контактов через браузер включайте
только при наличии разрешения оператора.

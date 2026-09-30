# support-desk

Короткое описание: что делает проект и зачем.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # заполнить своими значениями
support-desk           # или: python -m support_desk
```

## Тесты и линтер

```bash
pytest
ruff check .
```

## Структура

```
src/support_desk/   код
tests/              тесты
```

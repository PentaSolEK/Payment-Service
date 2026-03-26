# Payment Service

RESTful сервис для работы с платежами по заказам. Написан на Python с использованием FastAPI, SQLAlchemy (async) и SQLite.

\---

## Архитектура

### Структура проекта

```
payment\_service/
├── app/
│   ├── main.py                   # FastAPI app, lifespan, seeding
│   ├── config.py                 # Настройки через pydantic-settings
│   ├── db/
│   │   └── database.py           # Async engine, session factory
│   ├── models/
│   │   ├── order.py              # Order, PaymentStatus
│   │   └── payment.py            # Payment (STI), CashPayment, AcquiringPayment
│   ├── schemas/
│   │   └── schemas.py            # Pydantic I/O схемы
│   ├── services/
│   │   ├── bank\_client.py        # HTTP-клиент bank.api (retry, error isolation)
│   │   └── payment\_service.py    # Вся бизнес-логика
│   └── api/v1/
│       ├── orders.py             # GET /orders, GET /orders/{id}
│       └── payments.py           # POST /payments, POST /payments/{id}/refund, POST /payments/{id}/sync
└── requirements.txt
```

### Ключевые архитектурные решения

#### 1\. Single-Table Inheritance (STI) для платежей

Требование «модели платежей разных типов не должны отличаться» реализовано через STI:

* Одна таблица `payments` с колонкой-дискриминатором `payment\_type`.
* Два Python-класса: `CashPayment` и `AcquiringPayment` — наследники `Payment`.
* Весь код, которому не важен тип (списки, суммы, статусы), работает с базовым `Payment`.
* Тип-специфичная логика (проверка bank\_status перед депозитом) инкапсулирована в подклассах.

#### 2\. Service Layer

`PaymentService` содержит всю бизнес-логику, полностью отделённую от HTTP:

* Вызывается из REST-ручек.
* Может вызываться из фоновых задач, вебхуков, CLI — без изменений.
* Dependency injection: `BankAPIClient` передаётся в конструктор, что упрощает тестирование.

#### 3\. Адаптер банка (Anti-Corruption Layer)

`BankAPIClient` изолирует приложение от внешней системы:

* Все HTTP-ошибки и ошибки банка конвертируются в `BankAPIError` / `BankPaymentNotFound`.
* Retry с экспоненциальным back-off для сетевых ошибок и 5xx.
* 4xx не ретраится (финальные ошибки).
* Приложение никогда не видит `httpx.TransportError` напрямую.

#### 4\. Согласование состояния с банком

Проблема: банк может изменить статус платежа без уведомления нашего приложения.

Решение:

* `sync\_acquiring\_payment(payment\_id)` — явная синхронизация. Вызывается вручную через API или из фонового cron/webhook-обработчика.
* При возврате платежа (`refund\_payment`) sync вызывается автоматически перед попыткой рефанда.
* Если банк подтвердил оплату — локальный статус переходит в `DEPOSITED`.
* Если банк отменил — в `REFUNDED`.
* Ошибки банка при sync логируются и **не** бросают исключение: локальный статус остаётся неизменным.

\---

## Схема БД

```
┌─────────────────────────────────────────┐
│                 orders                   │
├──────────────┬──────────────────────────┤
│ id           │ INTEGER PK               │
│ amount       │ NUMERIC(12,2) NOT NULL   │
│ payment\_status│ ENUM(unpaid,            │
│              │  partially\_paid, paid)   │
│ description  │ VARCHAR(255)             │
└──────────────┴──────────────────────────┘
          │ 1
          │
          │ N
┌─────────────────────────────────────────────────────────┐
│                      payments                            │
├──────────────────┬──────────────────────────────────────┤
│ id               │ INTEGER PK                           │
│ order\_id         │ INTEGER FK → orders.id               │
│ amount           │ NUMERIC(12,2) NOT NULL               │
│ payment\_type     │ ENUM(cash, acquiring) NOT NULL       │  ← STI discriminator
│ status           │ ENUM(pending, deposited, refunded)   │
│ created\_at       │ DATETIME                             │
│ updated\_at       │ DATETIME                             │
│ bank\_payment\_id  │ VARCHAR(128) NULL                    │  ← acquiring only
│ bank\_status      │ ENUM(created, confirmed,             │  ← acquiring only
│                  │  cancelled, refunded) NULL           │
│ bank\_paid\_at     │ DATETIME NULL                        │  ← acquiring only
└──────────────────┴──────────────────────────────────────┘
```

Поля `bank\_\*` — `NULL` для кассовых платежей. Добавление нового типа платежа с уникальными полями потребует либо добавления колонок, либо перехода на Class Table Inheritance.

\---

## REST API

### Заказы

|Метод|URL|Описание|
|-|-|-|
|GET|`/api/v1/orders/`|Список всех заказов|
|GET|`/api/v1/orders/{id}`|Заказ по ID|

### Платежи

|Метод|URL|Описание|
|-|-|-|
|GET|`/api/v1/payments/order/{order\_id}`|Платежи по заказу|
|POST|`/api/v1/payments/`|Создать и оплатить платёж|
|POST|`/api/v1/payments/{id}/refund`|Вернуть платёж|
|POST|`/api/v1/payments/{id}/sync`|Синхронизировать acquiring с банком|

### Пример: создать платёж (наличные)

```bash
POST /api/v1/payments/
{
  "order\_id": 1,
  "amount": "500.00",
  "payment\_type": "cash"
}
```

Ответ `201`:

```json
{
  "payment": {
    "id": 1, "order\_id": 1, "amount": "500.00",
    "payment\_type": "cash", "status": "deposited",
    "created\_at": "...", "updated\_at": "...",
    "bank\_payment\_id": null, "bank\_status": null, "bank\_paid\_at": null
  },
  "order": {
    "id": 1, "amount": "1500.00",
    "payment\_status": "partially\_paid", "description": "Office supplies"
  }
}
```

### Пример: возврат платежа

```bash
POST /api/v1/payments/1/refund
```

\---

## Запуск

```bash
cd payment\_service
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Swagger UI: http://localhost:8000/docs

### Переменные окружения

|Переменная|По умолчанию|Описание|
|-|-|-|
|`DATABASE\_URL`|`sqlite+aiosqlite:///./payment\_service.db`|URL базы данных|
|`BANK\_API\_BASE\_URL`|`https://bank.api`|Базовый URL банка|
|`BANK\_API\_TIMEOUT`|`10.0`|Таймаут запросов к банку (сек)|
|`BANK\_API\_RETRIES`|`3`|Число повторных попыток при ошибке банка|

\---


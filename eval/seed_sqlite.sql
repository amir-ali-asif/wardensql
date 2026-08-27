-- SQLite-friendly mirror of sql/setup.sql (same tables, same rows).
-- No SERIAL/NUMERIC/roles: SQLite uses INTEGER PRIMARY KEY and dynamic typing.

CREATE TABLE customers (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    country    TEXT NOT NULL,
    ssn        TEXT,
    phone      TEXT,
    created_at TEXT
);

CREATE TABLE employees (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    department TEXT NOT NULL,
    ssn        TEXT,
    salary     REAL
);

CREATE TABLE products (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    category TEXT NOT NULL,
    price    REAL NOT NULL
);

CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    status      TEXT NOT NULL,
    placed_at   TEXT
);

CREATE TABLE order_items (
    id         INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity   INTEGER NOT NULL
);

INSERT INTO customers (id, name, country, ssn, phone) VALUES
    (1, 'Alice', 'US', '111-11-1111', '+1-202-555-0101'),
    (2, 'Bob',   'UK', '222-22-2222', '+44-20-5550-0102'),
    (3, 'Carla', 'PK', '333-33-3333', '+92-42-5550-0103'),
    (4, 'Diego', 'ES', '444-44-4444', '+34-91-5550-0104'),
    (5, 'Eve',   'US', '555-55-5555', '+1-202-555-0105');

INSERT INTO employees (id, name, department, ssn, salary) VALUES
    (1, 'Frank', 'sales',       '666-66-6666', 65000.00),
    (2, 'Grace', 'engineering', '777-77-7777', 98000.00),
    (3, 'Heidi', 'support',     '888-88-8888', 54000.00);

INSERT INTO products (id, name, category, price) VALUES
    (1, 'Keyboard', 'electronics', 49.99),
    (2, 'Mouse',    'electronics', 19.99),
    (3, 'Desk',     'furniture',   199.00),
    (4, 'Chair',    'furniture',   129.50),
    (5, 'Notebook', 'stationery',  4.99);

INSERT INTO orders (id, customer_id, status, placed_at) VALUES
    (1, 1, 'completed', '2024-01-01'),
    (2, 1, 'completed', '2024-01-08'),
    (3, 2, 'pending',   '2024-01-10'),
    (4, 3, 'completed', '2024-01-20'),
    (5, 5, 'cancelled', '2024-01-15');

INSERT INTO order_items (id, order_id, product_id, quantity) VALUES
    (1, 1, 1, 1), (2, 1, 2, 2), (3, 2, 3, 1),
    (4, 3, 5, 10), (5, 4, 4, 2), (6, 4, 2, 1);

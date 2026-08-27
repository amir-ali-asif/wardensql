-- Demo database + read-only role (the REAL guardrail).
--
--   psql -f sql/setup.sql            (run against the target database)
--
-- Includes a deliberately sensitive column (customers.ssn) to demonstrate the
-- governance layer: even though the role can read it, the app's deny-list refuses
-- any query that references it. See DENIED_COLUMNS in .env.example.

CREATE TABLE IF NOT EXISTS customers (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    country    TEXT NOT NULL,
    ssn        TEXT,                       -- sensitive: governance-denied
    phone      TEXT,                       -- sensitive: governance-denied (optional)
    created_at DATE NOT NULL DEFAULT CURRENT_DATE
);

-- A second table that ALSO has an `ssn` column, to demonstrate table-specific ABAC:
-- customers.ssn is denied but employees.ssn is NOT, and the validator resolves
-- aliases (e -> employees) to keep those decisions independent.
CREATE TABLE IF NOT EXISTS employees (
    id         SERIAL PRIMARY KEY,
    name       TEXT NOT NULL,
    department TEXT NOT NULL,
    ssn        TEXT,                       -- NOT denied (different table than customers)
    salary     NUMERIC(10, 2)
);

CREATE TABLE IF NOT EXISTS products (
    id       SERIAL PRIMARY KEY,
    name     TEXT NOT NULL,
    category TEXT NOT NULL,
    price    NUMERIC(10, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(id),
    status      TEXT NOT NULL,
    placed_at   DATE NOT NULL DEFAULT CURRENT_DATE
);

CREATE TABLE IF NOT EXISTS order_items (
    id         SERIAL PRIMARY KEY,
    order_id   INT NOT NULL REFERENCES orders(id),
    product_id INT NOT NULL REFERENCES products(id),
    quantity   INT NOT NULL
);

INSERT INTO customers (name, country, ssn, phone) VALUES
    ('Alice', 'US', '111-11-1111', '+1-202-555-0101'),
    ('Bob', 'UK', '222-22-2222', '+44-20-5550-0102'),
    ('Carla', 'PK', '333-33-3333', '+92-42-5550-0103'),
    ('Diego', 'ES', '444-44-4444', '+34-91-5550-0104'),
    ('Eve', 'US', '555-55-5555', '+1-202-555-0105')
ON CONFLICT DO NOTHING;

INSERT INTO employees (name, department, ssn, salary) VALUES
    ('Frank', 'sales', '666-66-6666', 65000.00),
    ('Grace', 'engineering', '777-77-7777', 98000.00),
    ('Heidi', 'support', '888-88-8888', 54000.00)
ON CONFLICT DO NOTHING;

INSERT INTO products (name, category, price) VALUES
    ('Keyboard','electronics',49.99), ('Mouse','electronics',19.99),
    ('Desk','furniture',199.00), ('Chair','furniture',129.50),
    ('Notebook','stationery',4.99)
ON CONFLICT DO NOTHING;

INSERT INTO orders (customer_id, status, placed_at) VALUES
    (1,'completed',CURRENT_DATE-10), (1,'completed',CURRENT_DATE-3),
    (2,'pending',CURRENT_DATE-1), (3,'completed',CURRENT_DATE-20),
    (5,'cancelled',CURRENT_DATE-5)
ON CONFLICT DO NOTHING;

INSERT INTO order_items (order_id, product_id, quantity) VALUES
    (1,1,1),(1,2,2),(2,3,1),(3,5,10),(4,4,2),(4,2,1)
ON CONFLICT DO NOTHING;

-- ---------- read-only role ----------
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'text2sql_ro') THEN
        CREATE ROLE text2sql_ro LOGIN PASSWORD 'readonly';
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO text2sql_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO text2sql_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO text2sql_ro;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON ALL TABLES IN SCHEMA public FROM text2sql_ro;
ALTER ROLE text2sql_ro SET statement_timeout = '5s';

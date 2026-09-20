-- A second database for the test suite, beside the real one.
--
-- The suite gives every test its own schema, so it could share a database
-- with the running server and still be isolated -- but sharing means a
-- mistake in a fixture is a mistake in somebody's history, and `DROP SCHEMA`
-- is one typo away from the wrong schema. A separate database costs nothing
-- and removes the question.
--
-- Runs only on an empty data directory, which is how PostgreSQL's entrypoint
-- works. An existing volume needs it by hand:
--
--     docker compose exec postgres createdb -U profitdog profitdog_test

SELECT 'CREATE DATABASE profitdog_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'profitdog_test')\gexec

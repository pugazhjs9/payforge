-- Runs once, the first time the MySQL volume is created (as root).
-- The main database "payforge" and user "payforge" come from the MYSQL_* variables;
-- tests use a separate database.
CREATE DATABASE IF NOT EXISTS payforge_test;
GRANT ALL PRIVILEGES ON payforge_test.* TO 'payforge'@'%';

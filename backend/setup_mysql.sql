-- Replace CHANGE_ME with your password (and use the same one in IDS_DATABASE_URL).
-- Run once as a MySQL admin:  sudo mysql < backend/setup_mysql.sql
CREATE DATABASE IF NOT EXISTS validation_identity CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'santi'@'localhost' IDENTIFIED BY 'CHANGE_ME';
CREATE USER IF NOT EXISTS 'santi'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME';
GRANT ALL PRIVILEGES ON validation_identity.* TO 'santi'@'localhost';
GRANT ALL PRIVILEGES ON validation_identity.* TO 'santi'@'127.0.0.1';
FLUSH PRIVILEGES;
-- Tables (events, results, identity_records) are created by the app on startup.

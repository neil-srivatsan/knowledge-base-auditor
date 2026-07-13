-- Create the dedicated test database alongside the default dev database.
-- Both are owned by the kbaudit user created by POSTGRES_USER.
-- This script runs automatically when the container first initialises.
CREATE DATABASE kbaudit_test;

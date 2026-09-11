-- dev / локальные
CREATE DATABASE IF NOT EXISTS wc_system_default CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS wc_system_archive CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- prod (DB_NAME=wc_system_prod / DB_NAME_ARCHIVE=wc_system_prod_archive)
CREATE DATABASE IF NOT EXISTS wc_system_prod CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS wc_system_prod_archive CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
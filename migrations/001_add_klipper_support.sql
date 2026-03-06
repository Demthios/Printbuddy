-- ============================================================================
-- KLIPPER INTEGRATION — DATABASE MIGRATION
-- ============================================================================
-- Run this against your bambuddy.db SQLite file ONCE.
-- Back up the DB first: cp bambuddy.db bambuddy.db.bak
--
-- SQLite doesn't support ALTER TABLE ADD COLUMN with constraints on existing
-- tables cleanly, so we add nullable columns and let the app fill them in.
-- ============================================================================

-- Step 1: Add printer_type discriminator
--   Existing rows will get NULL here; the printer manager should default-treat
--   NULL as "bambu" for backward compatibility.
ALTER TABLE printers ADD COLUMN printer_type TEXT DEFAULT 'bambu';

-- Step 2: Add Moonraker connection fields (Klipper-only)
ALTER TABLE printers ADD COLUMN moonraker_host TEXT;
ALTER TABLE printers ADD COLUMN moonraker_port INTEGER DEFAULT 7125;
ALTER TABLE printers ADD COLUMN moonraker_api_key TEXT;
ALTER TABLE printers ADD COLUMN klipper_camera_url TEXT;
ALTER TABLE printers ADD COLUMN klipper_upload_subfolder TEXT DEFAULT 'printbuddy';

-- Step 3: Update existing Bambu rows so they're explicit
UPDATE printers SET printer_type = 'bambu' WHERE printer_type IS NULL;

-- ============================================================================
-- VERIFICATION — run these selects to confirm the migration worked:
-- ============================================================================
-- SELECT id, name, printer_type FROM printers;
-- PRAGMA table_info(printers);

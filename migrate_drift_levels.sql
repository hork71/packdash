-- Per-OS-release drift levels: drift groups change from (package, os)
-- to (package, os, os_release), e.g. RedHat 7/8/9 each get their own
-- newest version. Run once on databases created before this change,
-- then re-run import.py (or drift.py) to fill the new levels.

ALTER TABLE servers
ADD COLUMN os_release VARCHAR(20) NOT NULL DEFAULT 'unknown';

UPDATE servers SET os_release = CASE
    WHEN osversie IS NULL OR osversie = '' THEN 'unknown'
    WHEN os IN ('RedHat', 'SLES', 'SUSE') THEN split_part(osversie, '.', 1)
    ELSE osversie
END;

CREATE INDEX idx_servers_os_release ON servers(os, os_release);

DELETE FROM package_drift;

ALTER TABLE package_drift
ADD COLUMN os_release VARCHAR(20) NOT NULL DEFAULT 'unknown';

ALTER TABLE package_drift ALTER COLUMN os_release DROP DEFAULT;

ALTER TABLE package_drift DROP CONSTRAINT package_drift_pkey;

ALTER TABLE package_drift ADD PRIMARY KEY (package_id, os, os_release);

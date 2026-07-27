-- Migration for existing databases: drift materialization.
-- Fresh installs get this from setup.sql; run this once on databases
-- created before it, then run import.py (or drift.py) to fill it.

CREATE TABLE package_drift (
    package_id BIGINT NOT NULL REFERENCES packages(id),
    os VARCHAR(20) NOT NULL,
    latest_version_id BIGINT NOT NULL REFERENCES package_versions(id),

    -- counts over ACTIVE servers only
    version_count INTEGER NOT NULL,
    server_count INTEGER NOT NULL,
    behind_count INTEGER NOT NULL,

    PRIMARY KEY (package_id, os)
);

ALTER TABLE server_packages
ADD COLUMN is_latest BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX idx_package_drift_drifting
    ON package_drift(behind_count DESC) WHERE version_count > 1;

CREATE INDEX idx_server_packages_not_latest
    ON server_packages(server_id) WHERE NOT is_latest;

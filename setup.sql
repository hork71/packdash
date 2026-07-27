CREATE TABLE servers (
    id UUID PRIMARY KEY,
    hostname VARCHAR(255) NOT NULL,
    beheergroep VARCHAR(100),
    beheeremail VARCHAR(255),
    owner VARCHAR(100),
    servicelevel VARCHAR(20),
    os VARCHAR(20),
    osversie VARCHAR(20),
    suma BOOLEAN,
    apiversie VARCHAR(20)
);

CREATE TABLE packages (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE package_versions (
    id BIGSERIAL PRIMARY KEY,
    package_id BIGINT NOT NULL REFERENCES packages(id),

    version VARCHAR(100) NOT NULL,
    release VARCHAR(100) NOT NULL,
    arch VARCHAR(50) NOT NULL,

    UNIQUE (
        package_id,
        version,
        release,
        arch
    )
);

CREATE TABLE server_packages (
    server_id UUID NOT NULL REFERENCES servers(id),
    package_version_id BIGINT NOT NULL REFERENCES package_versions(id),

    install_time TIMESTAMPTZ,

    PRIMARY KEY (
        server_id,
        package_version_id
    )
);

CREATE TABLE inventory_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    source VARCHAR(100),
    server_count INTEGER DEFAULT 0
);

ALTER TABLE servers
ADD COLUMN inventory_status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
    CHECK (inventory_status IN ('ACTIVE','MISSING','DECOMMISSIONED'));

ALTER TABLE servers
ADD COLUMN last_seen TIMESTAMPTZ;

ALTER TABLE servers
ADD COLUMN last_inventory_run BIGINT
REFERENCES inventory_runs(id);

CREATE INDEX idx_server_packages_pv ON server_packages(package_version_id);
CREATE INDEX idx_package_versions_pkg ON package_versions(package_id);
CREATE INDEX idx_servers_beheergroep ON servers(beheergroep);
CREATE INDEX idx_servers_status ON servers(inventory_status);

-- Drift materialization (see migrate_drift.sql for existing databases):
-- filled by drift.py after every import, read by the API.

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

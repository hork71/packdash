-- Migration: SUMA channel package_id passthrough + security advisory
-- data (errata/CVEs) for the fleet's newest drifting package versions.
-- Run once against an existing database; setup.sql already includes
-- this for fresh installs.

ALTER TABLE server_packages
ADD COLUMN suma_package_id INTEGER;

-- errata/errata_cves/package_version_errata are filled by advisories.py
-- (run after import.py, once drift has been materialized) and read by
-- the API. package_version_advisory_check records that a version has
-- been checked at all, even when no advisory was found, so a version
-- with no known advisory is not re-queried on every run.

CREATE TABLE errata (
    advisory_name  VARCHAR(50) PRIMARY KEY,
    advisory_id    VARCHAR(50),
    advisory_type  VARCHAR(50),
    synopsis       TEXT,
    severity       VARCHAR(20),
    issue_date     TIMESTAMPTZ,
    suma_source    VARCHAR(20),
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE errata_cves (
    advisory_name  VARCHAR(50) NOT NULL REFERENCES errata(advisory_name) ON DELETE CASCADE,
    cve            VARCHAR(20) NOT NULL,

    PRIMARY KEY (advisory_name, cve)
);

CREATE TABLE package_version_errata (
    package_version_id BIGINT NOT NULL REFERENCES package_versions(id) ON DELETE CASCADE,
    advisory_name       VARCHAR(50) NOT NULL REFERENCES errata(advisory_name) ON DELETE CASCADE,

    PRIMARY KEY (package_version_id, advisory_name)
);

CREATE INDEX idx_package_version_errata_pv ON package_version_errata(package_version_id);

CREATE TABLE package_version_advisory_check (
    package_version_id BIGINT PRIMARY KEY REFERENCES package_versions(id) ON DELETE CASCADE,
    checked_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- provenance: which channel package id / SUMA instance the check
    -- used, or NULL if no server reported a usable channel package id
    -- for this version at all (nothing to check against).
    suma_package_id     INTEGER,
    suma_source         VARCHAR(20)
);

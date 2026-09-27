-- Agents from 1.14.2 report the installed package (core: server monitoring;
-- full: with APM) and their effective monitoring profile, so Agent Fleet can
-- show both and switch servers between server monitoring and APM.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS package_flavor varchar(16);
ALTER TABLE agents ADD COLUMN IF NOT EXISTS monitoring_profile varchar(32);

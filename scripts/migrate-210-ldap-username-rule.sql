ALTER TABLE users ADD COLUMN IF NOT EXISTS ldap_username_rule_required boolean NOT NULL DEFAULT false;

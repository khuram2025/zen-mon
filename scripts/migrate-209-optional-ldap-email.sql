-- LDAP users can have no directory email. Local and other accounts still
-- require an email, and the existing unique constraint protects populated addresses.
ALTER TABLE users ALTER COLUMN email DROP NOT NULL;

DO $$ BEGIN
    ALTER TABLE users ADD CONSTRAINT users_email_required_for_non_ldap
        CHECK (email IS NOT NULL OR auth_source = 'ldap');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

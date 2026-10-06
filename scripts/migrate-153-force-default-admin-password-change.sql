-- Appliances installed with install.sh are seeded with admin / admin123, and
-- until now nothing made the administrator change it. Require a new password at
-- the next sign-in for exactly those accounts that still carry the seeded hash.
--
-- A password the customer chose has a different (salted) hash and is never
-- touched. The API refuses every request from a flagged account except the
-- password change, and the dashboard shows the forced-change screen.
UPDATE users
   SET must_change_password = TRUE,
       updated_at = NOW()
 WHERE username = 'admin'
   AND password_hash = '$2b$12$vjHI8XBgL.dCyn.sgl41VufIFkQGcEzjt78GJdB66AwG9e9MZasai'
   AND must_change_password = FALSE;

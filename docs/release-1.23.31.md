# ZenPlus 1.23.31 — SSH terminal looks and works like PuTTY

The Settings > General > Monitoring SSH terminal (added in 1.23.30) now shows
its black terminal window at all times, like an empty PuTTY window, instead of
only after Connect. The connection fields sit above it, Connect becomes
Disconnect while a session is open, and the status line shows the connection
state. Colours and block cursor follow PuTTY's defaults; the window refits when
the page is resized. Session messages (closed, timed out, failed login) are
printed in the terminal itself.

No server, schema or permission changes. Includes 1.23.29 and 1.23.30.
Appliance-only, minimum prerequisite 1.23.9.

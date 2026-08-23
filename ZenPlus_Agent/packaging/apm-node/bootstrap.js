'use strict'

// Loaded through NODE_OPTIONS before the application entry point. Keeping the
// bootstrap in the signed ZenPlus runtime pack means target applications do
// not need npm, internet access, or source-code changes.
require('@opentelemetry/auto-instrumentations-node/register')

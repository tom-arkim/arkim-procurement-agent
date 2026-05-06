# Compatibility shim — redirects utils.sourcing.* to utils.sourcing_archieved.*
# Required because the preserved modules contain internal imports that reference
# the old package path. Do not add public API here; callers use sourcing_archieved
# directly. This shim exists only to satisfy intra-module imports.

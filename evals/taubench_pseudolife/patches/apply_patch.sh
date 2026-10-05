#!/usr/bin/env sh
# Apply the Pseudolife hooks to a taubench checkout. Run from the checkout root
# (the directory containing src/tau2). The checkout was unpacked from a tarball
# and has no .git, so GNU patch is the primary tool.
set -eu
patch -p1 < "$(dirname "$0")/tau2_pseudolife.patch"

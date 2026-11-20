# Native make shim. The canonical build is the GNU Makefile.
GMAKE ?= gmake

.PHONY: all build test run clean liboqs generate check-generated test-ziran test-ziran-ir compression-bench

.MAIN: all

all:
	$(GMAKE) -f Makefile $@

build test run clean liboqs generate check-generated test-ziran test-ziran-ir compression-bench:
	$(GMAKE) -f Makefile $@

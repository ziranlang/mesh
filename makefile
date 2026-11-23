# Native make shim. The canonical build is the GNU Makefile.
GMAKE ?= gmake

.PHONY: all build test run clean liboqs generate check-generated build-ziran-native-server build-ziran-native-embedded test-ziran test-ziran-ir test-ziran-native test-ziran-native-sqlite test-ziran-native-http test-ziran-native-http-coding test-ziran-native-ml-dsa44 test-ziran-native-account-auth test-ziran-native-server build-native-checks test-ziran-native-google-play test-ziran-native-ledger test-ziran-native-embedded sqlite-native microhttpd-native native-release build-native compression-bench

.MAIN: all

all:
	$(GMAKE) -f Makefile $@

build test run clean liboqs generate check-generated build-ziran-native-server build-ziran-native-embedded test-ziran test-ziran-ir test-ziran-native test-ziran-native-sqlite test-ziran-native-http test-ziran-native-http-coding test-ziran-native-ml-dsa44 test-ziran-native-account-auth test-ziran-native-server build-native-checks test-ziran-native-google-play test-ziran-native-ledger test-ziran-native-embedded sqlite-native microhttpd-native native-release build-native compression-bench:
	$(GMAKE) -f Makefile $@

LIBOQS_DIR ?= vendor/liboqs
BUILD_DIR := build
LIBOQS_BUILD_DIR := $(BUILD_DIR)/liboqs
LIBOQS_PREFIX := $(LIBOQS_BUILD_DIR)/install
LIBOQS_A := $(LIBOQS_PREFIX)/lib/liboqs.a

GO ?= go
CMAKE ?= cmake
GOFLAGS ?= -mod=mod
VERSION ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
SOURCE_COMMIT ?= $(shell git rev-parse HEAD 2>/dev/null || echo unknown)
LIBOQS_COMMIT ?= $(shell git -C $(LIBOQS_DIR) rev-parse HEAD 2>/dev/null || echo unknown)
NATIVE_SERVER_BUILD_DIR ?= $(BUILD_DIR)/native-tests/server
NATIVE_EMBEDDED_BUILD_DIR ?= $(BUILD_DIR)/native-tests/embedded
NATIVE_SERVER_VERSION = $(if $(filter file,$(origin VERSION)),$(or $(filter-out unknown,$(SOURCE_COMMIT)),$(VERSION)),$(VERSION))
NATIVE_SERVER_LIBS := -lmicrohttpd -lsqlite3 -loqs -lcrypto -l:libcurl.so.4 -lzstd -lz -lpcre2-8 -lm -ldl -pthread
DEPLOYMENT_MANIFEST := $(BUILD_DIR)/mesh.manifest.json
LIBOQS_CMAKE_OPTIONS := -DCMAKE_INSTALL_PREFIX=$(abspath $(LIBOQS_PREFIX)) \
	-DCMAKE_INSTALL_LIBDIR=lib -DBUILD_SHARED_LIBS=OFF \
	-DOQS_BUILD_ONLY_LIB=ON -DOQS_USE_OPENSSL=OFF \
	-DOQS_MINIMAL_BUILD=SIG_ml_dsa_44

CGO_ENV := CGO_ENABLED=1 \
	GOFLAGS="$(GOFLAGS)" \
	CGO_CFLAGS="-I$(abspath $(LIBOQS_PREFIX))/include" \
	CGO_LDFLAGS="-L$(abspath $(LIBOQS_PREFIX))/lib -loqs"

.PHONY: all build test run clean liboqs generate build-ziran-native-server build-ziran-native-embedded test-ziran test-ziran-ir test-ziran-native test-ziran-native-sqlite test-ziran-native-http test-ziran-native-http-coding test-ziran-native-ml-dsa44 test-ziran-native-account-auth test-ziran-native-server test-ziran-native-google-play test-ziran-native-ledger sqlite-native microhttpd-native test-ziran-native-embedded compression-bench

all: build

# The Go is generated from the Zi sources and never committed; builds
# regenerate it when a source or the pinned toolchain changes. CI and the
# deploy generate it with the pinned Ziran before building.
ZIRAN ?= ziran
ZIRAN_PROJECT := --project $(if $(wildcard ziran.local.toml),,--locked)
GENERATED := ziran_entry.zi.go
generate:
	ZIRAN="$(ZIRAN)" python3 scripts/generate_go.py

ZI_SOURCES := $(shell find src tests/ziran -type f -name '*.zi')

$(GENERATED): $(ZI_SOURCES) ziran.toml ziran.lock scripts/generate_go.py
	ZIRAN="$(ZIRAN)" python3 scripts/generate_go.py

test-ziran: test test-ziran-ir test-ziran-native

test-ziran-ir: liboqs $(GENERATED)
	$(CGO_ENV) ZIRAN="$(ZIRAN)" GOCACHE=$(abspath $(BUILD_DIR)/gocache) python3 scripts/test_saved_ir.py

test-ziran-native:
	mkdir -p $(BUILD_DIR)/native-tests
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
	$(ZIRAN) build $(ZIRAN_PROJECT) \
		--target=c --exe --entry primitives:Main --root tests/native \
		-o $(BUILD_DIR)/native-tests \
		tests/native/primitives.zi
	$(BUILD_DIR)/native-tests/primitives

SQLITE_NATIVE_DIR ?= $(abspath ../packages/sqlite/build/native)
SQLITE_BUILD_DIR := $(BUILD_DIR)/sqlite3
SQLITE_LOCKED_DIR := $(BUILD_DIR)/sqlite-native
MICROHTTPD_VERSION := 1.0.1-4
MICROHTTPD_RUNTIME_SHA256 := e4161f63d26b4835a79b936f50b4cc90fb10b718f3b6623ac609856adde4c76b
MICROHTTPD_DEV_SHA256 := 4bfcff352f603d881683339b0faf55de1b1c4fc492d17741f400264851852b35
MICROHTTPD_ROOT := $(BUILD_DIR)/microhttpd

# SQLite from the locked sqlite3 source, built outside its package checkout.
# Production links this amalgamation, so the native suites use it too.
sqlite-native: $(SQLITE_LOCKED_DIR)/libsqlite3.so

$(SQLITE_LOCKED_DIR)/libsqlite3.so: ziran.lock
	mkdir -p $(SQLITE_BUILD_DIR) $(SQLITE_LOCKED_DIR)
	set -e; \
	source=$$($(ZIRAN) pkg path sqlite3 $(if $(wildcard ziran.local.toml),,--locked)); \
	test -x "$$source/configure"; \
	cd $(SQLITE_BUILD_DIR) && "$$source/configure" --disable-shared --disable-readline --disable-tcl
	$(MAKE) -C $(SQLITE_BUILD_DIR) sqlite3.c
	$(CC) -O2 -fPIC -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 \
		-c $(SQLITE_BUILD_DIR)/sqlite3.c -o $(SQLITE_LOCKED_DIR)/sqlite3.o
	$(CC) -shared $(SQLITE_LOCKED_DIR)/sqlite3.o -lm -ldl -pthread -o $@

# Debian's libmicrohttpd, the package the production origin installs.
microhttpd-native:
	mkdir -p $(MICROHTTPD_ROOT)/root
	set -e; \
	for entry in libmicrohttpd12t64:$(MICROHTTPD_RUNTIME_SHA256) libmicrohttpd-dev:$(MICROHTTPD_DEV_SHA256); do \
		package=$${entry%%:*}; \
		digest=$${entry#*:}; \
		file="$(MICROHTTPD_ROOT)/$${package}_$(MICROHTTPD_VERSION)_amd64.deb"; \
		if [ ! -f "$$file" ]; then \
			curl --fail --location --silent --show-error --output "$$file.part" \
				"https://deb.debian.org/debian/pool/main/libm/libmicrohttpd/$${package}_$(MICROHTTPD_VERSION)_amd64.deb"; \
			mv "$$file.part" "$$file"; \
		fi; \
		printf '%s  %s\n' "$$digest" "$$file" | sha256sum --check --strict --quiet; \
		dpkg-deb -x "$$file" $(MICROHTTPD_ROOT)/root; \
	done

test-ziran-native-sqlite:
	mkdir -p $(BUILD_DIR)/native-tests/sqlite-health
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
	LDFLAGS="-L$(SQLITE_NATIVE_DIR)" LDLIBS="-lsqlite3 -lm -ldl -pthread" \
	$(ZIRAN) build $(ZIRAN_PROJECT) \
		--target=c --exe --entry sqlite_health:Main --root tests/native \
		-o $(BUILD_DIR)/native-tests/sqlite-health tests/native/sqlite_health.zi
	set -e; \
	fixture=$$(mktemp -d "$(abspath $(BUILD_DIR))/native-tests/sqlite-health/db.XXXXXX"); \
	trap 'rm -rf "$$fixture"' EXIT; \
	MESH_TEST_SQLITE_PATH="$$fixture/mesh.db" \
	LD_LIBRARY_PATH="$(SQLITE_NATIVE_DIR)$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
		$(BUILD_DIR)/native-tests/sqlite-health/sqlite_health

test-ziran-native-http: liboqs
	mkdir -p $(BUILD_DIR)/native-tests/http-listener
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
	LDFLAGS="-L$(abspath $(LIBOQS_PREFIX))/lib -L$(SQLITE_NATIVE_DIR) $(if $(MICROHTTPD_NATIVE_DIR),-L$(MICROHTTPD_NATIVE_DIR))" \
	LDLIBS="-lmicrohttpd -lsqlite3 -loqs -lcrypto -lzstd -lz -lm -ldl -pthread" \
	$(ZIRAN) build $(ZIRAN_PROJECT) \
		--target=c --exe --entry http_listener:Main --root tests/native \
		-o $(BUILD_DIR)/native-tests/http-listener tests/native/http_listener.zi
	$(CGO_ENV) GOCACHE=$(abspath $(BUILD_DIR)/gocache) $(GO) build \
		-o $(BUILD_DIR)/native-tests/http-listener/signing_oracle ./testdata/ml_dsa44
	LD_LIBRARY_PATH="$(SQLITE_NATIVE_DIR)$(if $(MICROHTTPD_NATIVE_DIR),:$(MICROHTTPD_NATIVE_DIR))$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
		python3 -B tests/native/check_http_listener.py $(BUILD_DIR)/native-tests/http-listener/http_listener \
		$(BUILD_DIR)/native-tests/http-listener/signing_oracle
	mkdir -p $(BUILD_DIR)/native-tests/http-policy
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
	LDFLAGS="$(LDFLAGS) $(if $(MICROHTTPD_NATIVE_DIR),-L$(MICROHTTPD_NATIVE_DIR))" LDLIBS="$(LDLIBS) -lmicrohttpd" \
	$(ZIRAN) build $(ZIRAN_PROJECT) --target=c --exe --entry http_policy:Main --root tests/native \
		-o $(BUILD_DIR)/native-tests/http-policy tests/native/http_policy.zi
	LD_LIBRARY_PATH="$(MICROHTTPD_NATIVE_DIR)$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
		python3 -B tests/native/check_http_policy.py $(BUILD_DIR)/native-tests/http-policy/http_policy

test-ziran-native-http-coding:
	mkdir -p $(BUILD_DIR)/native-tests/http-coding
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
	LDFLAGS="$(LDFLAGS) $(if $(MICROHTTPD_NATIVE_DIR),-L$(MICROHTTPD_NATIVE_DIR))" \
	LDLIBS="$(LDLIBS) -lmicrohttpd -lzstd -lz -pthread" \
	$(ZIRAN) build $(ZIRAN_PROJECT) \
		--target=c --exe --entry http_coding:Main --root tests/native \
		-o $(BUILD_DIR)/native-tests/http-coding tests/native/http_coding.zi
	LD_LIBRARY_PATH="$(MICROHTTPD_NATIVE_DIR)$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
		python3 -B tests/native/check_http_coding.py $(BUILD_DIR)/native-tests/http-coding/http_coding

build-ziran-native-server: export MESH_NATIVE_BUILD_VERSION = $(NATIVE_SERVER_VERSION)
build-ziran-native-server: liboqs
	mkdir -p "$(NATIVE_SERVER_BUILD_DIR)"
	python3 -B scripts/write_native_entry.py "$(NATIVE_SERVER_BUILD_DIR)/mesh_native_entry.zi"
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
	LDFLAGS="$(LDFLAGS) -L$(abspath $(LIBOQS_PREFIX))/lib -L$(SQLITE_NATIVE_DIR) $(if $(MICROHTTPD_NATIVE_DIR),-L$(MICROHTTPD_NATIVE_DIR))" \
	LDLIBS="$(NATIVE_SERVER_LIBS)" \
	$(ZIRAN) build $(ZIRAN_PROJECT) --module-path "$(NATIVE_SERVER_BUILD_DIR)" \
		--target=c --exe --entry mesh_native_entry:Main --root src/server \
		-o "$(NATIVE_SERVER_BUILD_DIR)" "$(NATIVE_SERVER_BUILD_DIR)/mesh_native_entry.zi"
	mv "$(NATIVE_SERVER_BUILD_DIR)/mesh_native_entry" "$(NATIVE_SERVER_BUILD_DIR)/server_native"

test-ziran-native-server: export MESH_TEST_BUILD_VERSION = $(NATIVE_SERVER_VERSION)
test-ziran-native-server: test-ziran-native-http
	$(MAKE) -f Makefile build-ziran-native-server
	LD_LIBRARY_PATH="$(SQLITE_NATIVE_DIR)$(if $(MICROHTTPD_NATIVE_DIR),:$(MICROHTTPD_NATIVE_DIR))$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
		python3 -B tests/native/check_http_listener.py "$(NATIVE_SERVER_BUILD_DIR)/server_native" \
		$(BUILD_DIR)/native-tests/http-listener/signing_oracle --server
	LD_LIBRARY_PATH="$(SQLITE_NATIVE_DIR)$(if $(MICROHTTPD_NATIVE_DIR),:$(MICROHTTPD_NATIVE_DIR))$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
		python3 -B tests/native/check_account_2fa.py "$(NATIVE_SERVER_BUILD_DIR)/server_native" \
		$(BUILD_DIR)/native-tests/http-listener/signing_oracle
	$(MAKE) -f Makefile test-ziran-native-google-play
	$(MAKE) -f Makefile test-ziran-native-ledger

# Signed ledger allocation and referral settlement against in-memory SQLite.
NATIVE_LEDGER_TESTS := token_payment_ledger referral_settlement
test-ziran-native-ledger: liboqs
	set -e; \
	for entry in $(NATIVE_LEDGER_TESTS); do \
		mkdir -p "$(BUILD_DIR)/native-tests/$$entry"; \
		XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
		LDFLAGS="$(LDFLAGS) -L$(abspath $(LIBOQS_PREFIX))/lib -L$(SQLITE_NATIVE_DIR) $(if $(MICROHTTPD_NATIVE_DIR),-L$(MICROHTTPD_NATIVE_DIR))" \
		LDLIBS="$(NATIVE_SERVER_LIBS)" \
		$(ZIRAN) build $(ZIRAN_PROJECT) --target=c --exe --entry "$$entry:Main" --root tests/native \
			-o "$(BUILD_DIR)/native-tests/$$entry" "tests/native/$$entry.zi"; \
		LD_LIBRARY_PATH="$(SQLITE_NATIVE_DIR)$(if $(MICROHTTPD_NATIVE_DIR),:$(MICROHTTPD_NATIVE_DIR))$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
			"$(BUILD_DIR)/native-tests/$$entry/$$entry"; \
	done

# Google Play verification, OAuth and refunds against a loopback Play API.
test-ziran-native-google-play: liboqs
	mkdir -p $(BUILD_DIR)/native-tests/google-play
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
	LDFLAGS="$(LDFLAGS) -L$(abspath $(LIBOQS_PREFIX))/lib -L$(SQLITE_NATIVE_DIR) $(if $(MICROHTTPD_NATIVE_DIR),-L$(MICROHTTPD_NATIVE_DIR))" \
	LDLIBS="$(NATIVE_SERVER_LIBS)" \
	$(ZIRAN) build $(ZIRAN_PROJECT) --target=c --exe --entry google_play:Main --root tests/native \
		-o $(BUILD_DIR)/native-tests/google-play tests/native/google_play.zi
	LD_LIBRARY_PATH="$(SQLITE_NATIVE_DIR)$(if $(MICROHTTPD_NATIVE_DIR),:$(MICROHTTPD_NATIVE_DIR))$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
		python3 -B tests/native/check_google_play.py $(BUILD_DIR)/native-tests/google-play/google_play \
		$(BUILD_DIR)/native-tests/http-listener/signing_oracle

# Load generated protocol types before SQL macros with matching field names.
build-ziran-native-embedded: liboqs
	mkdir -p "$(NATIVE_EMBEDDED_BUILD_DIR)"
	@set -eu; \
	generation=$$(mktemp -d "$(NATIVE_EMBEDDED_BUILD_DIR)/codegen.XXXXXX"); \
	trap 'rm -rf "$$generation"' EXIT; \
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
		$(ZIRAN) build $(ZIRAN_PROJECT) --target=c --no-main --root src/node \
		-o "$$generation" src/node/embedded_runtime_native.zi; \
	$(CC) $(CPPFLAGS) $(CFLAGS) -fPIC -ffunction-sections -fdata-sections \
		-Wl,--no-undefined,--gc-sections \
		-Wl,--version-script=native/mesh_embedded.map -Wl,-soname,libmesh_embedded.so \
		-I"$$generation" -include "$$generation/protocol.h" \
		"$$generation"/*.c native/embedded/allocation.c \
		$(LDFLAGS) -shared -L$(abspath $(LIBOQS_PREFIX))/lib -L$(SQLITE_NATIVE_DIR) \
		$(if $(MICROHTTPD_NATIVE_DIR),-L$(MICROHTTPD_NATIVE_DIR)) $(NATIVE_SERVER_LIBS) \
		-o "$$generation/libmesh_embedded.so"; \
	mv "$$generation/libmesh_embedded.so" "$(NATIVE_EMBEDDED_BUILD_DIR)/libmesh_embedded.so"

test-ziran-native-embedded: build-ziran-native-embedded
	$(CC) $(CPPFLAGS) $(CFLAGS) -std=c11 -Wall -Wextra -Werror -Inative tests/native/embedded_host.c \
		$(LDFLAGS) -L"$(NATIVE_EMBEDDED_BUILD_DIR)" -Xlinker -rpath-link -Xlinker "$(SQLITE_NATIVE_DIR)" \
		$(if $(MICROHTTPD_NATIVE_DIR),-Xlinker -rpath-link -Xlinker "$(MICROHTTPD_NATIVE_DIR)") -lmesh_embedded \
		-o "$(NATIVE_EMBEDDED_BUILD_DIR)/embedded_host"
	LD_LIBRARY_PATH="$(abspath $(NATIVE_EMBEDDED_BUILD_DIR)):$(SQLITE_NATIVE_DIR)$(if $(MICROHTTPD_NATIVE_DIR),:$(MICROHTTPD_NATIVE_DIR))$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
		python3 -B tests/native/check_embedded_host.py "$(NATIVE_EMBEDDED_BUILD_DIR)/embedded_host"

test-ziran-native-ml-dsa44: liboqs
	mkdir -p $(BUILD_DIR)/native-tests/ml-dsa44
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
	CFLAGS="$(CFLAGS) -ffunction-sections -fdata-sections" \
	LDFLAGS="$(LDFLAGS) -Wl,--gc-sections -L$(abspath $(LIBOQS_PREFIX))/lib" \
	LDLIBS="$(LDLIBS) -loqs" \
	$(ZIRAN) build $(ZIRAN_PROJECT) \
		--target=c --exe --entry ml_dsa44:Main --root tests/native \
		-o $(BUILD_DIR)/native-tests/ml-dsa44 tests/native/ml_dsa44.zi
	python3 -B tests/native/check_ml_dsa44.py $(BUILD_DIR)/native-tests/ml-dsa44/ml_dsa44

test-ziran-native-account-auth: liboqs
	mkdir -p $(BUILD_DIR)/native-tests/account-auth
	XDG_CACHE_HOME="$${XDG_CACHE_HOME:-$(abspath $(BUILD_DIR)/package-cache)}" \
	CFLAGS="$(CFLAGS) -ffunction-sections -fdata-sections" \
	LDFLAGS="$(LDFLAGS) -Wl,--gc-sections -L$(abspath $(LIBOQS_PREFIX))/lib -L$(SQLITE_NATIVE_DIR)" \
	LDLIBS="$(LDLIBS) -loqs -lsqlite3 -lcrypto -lm -ldl -pthread" \
	$(ZIRAN) build $(ZIRAN_PROJECT) \
		--target=c --exe --entry account_auth:Main --root tests/native \
		-o $(BUILD_DIR)/native-tests/account-auth tests/native/account_auth.zi
	LD_LIBRARY_PATH="$(SQLITE_NATIVE_DIR)$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}" \
		$(BUILD_DIR)/native-tests/account-auth/account_auth

BENCH_DIR := $(BUILD_DIR)/compression-bench
BENCH_SOURCES := $(wildcard bench/compression/*.zi)
BENCH_GENERATE := $(ZIRAN) build --target=go --pkg main --root bench/compression

compression-bench:
	set -e; \
	mkdir -p "$(BENCH_DIR)"; \
	generated=$$(mktemp -d "$(abspath $(BENCH_DIR))/go.XXXXXX"); \
	trap 'rm -rf "$$generated"' EXIT; \
	$(BENCH_GENERATE) --no-main -o "$$generated" $(BENCH_SOURCES); \
	$(BENCH_GENERATE) --exe --entry bench_main:Main -o "$$generated/entry" $(BENCH_SOURCES); \
	cp "$$generated/entry/ziran_entry.go" "$$generated/ziran_entry.go"; \
	cd bench/compression; \
	GOCACHE="$(abspath $(BUILD_DIR)/gocache)" $(GO) build \
		-o "$(abspath $(BENCH_DIR))/compression-bench" "$$generated"/*.go

liboqs:
	set -e; \
	status=0; \
	python3 -B scripts/verify_deployment_inputs.py check-native-build \
		--source-dir "$(LIBOQS_DIR)" --build-dir "$(LIBOQS_BUILD_DIR)" \
		--artifact "$(LIBOQS_A)" --cmake "$(CMAKE)" \
		--cmake-options "$(LIBOQS_CMAKE_OPTIONS)" || status=$$?; \
	if [ "$$status" -eq 0 ]; then \
		:; \
	elif [ "$$status" -eq 2 ]; then \
		rm -rf "$(LIBOQS_BUILD_DIR)"; \
		$(CMAKE) -S $(LIBOQS_DIR) -B $(LIBOQS_BUILD_DIR) $(LIBOQS_CMAKE_OPTIONS); \
		$(CMAKE) --build $(LIBOQS_BUILD_DIR) --target install; \
		python3 -B scripts/verify_deployment_inputs.py record-native-build \
			--source-dir "$(LIBOQS_DIR)" --build-dir "$(LIBOQS_BUILD_DIR)" \
			--artifact "$(LIBOQS_A)" --cmake "$(CMAKE)" \
			--cmake-options "$(LIBOQS_CMAKE_OPTIONS)"; \
	else \
		exit "$$status"; \
	fi

build: liboqs $(GENERATED)
	$(CGO_ENV) $(GO) build -ldflags "-X main.BuildVersion=$(VERSION)" -o mesh .
	$(CGO_ENV) python3 -B scripts/verify_deployment_inputs.py record-artifact-manifest \
		--output mesh --manifest "$(DEPLOYMENT_MANIFEST)" \
		--source-commit "$(SOURCE_COMMIT)" --liboqs-commit "$(LIBOQS_COMMIT)" \
		--native-metadata "$(LIBOQS_BUILD_DIR)/.mesh-native-build.json"

test: liboqs $(GENERATED)
	$(CGO_ENV) GO="$(GO)" GOCACHE=$(abspath $(BUILD_DIR)/gocache) python3 scripts/test_go.py $(TEST_FLAGS)

run: liboqs $(GENERATED)
	$(CGO_ENV) $(GO) run .

clean:
	rm -rf $(BUILD_DIR) mesh ksync *.zi.go *.zi_test.go

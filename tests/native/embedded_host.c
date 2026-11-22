#include "mesh_embedded.h"

#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define CHECK(expression) do { \
    if (!(expression)) { \
        fprintf(stderr, "embedded host line %d: %s\n", __LINE__, #expression); \
        return 1; \
    } \
} while (0)

static int request(long long handle, char *path, char *buffer, int capacity) {
    return NodeRequest(handle, "GET", path, NULL, buffer, capacity);
}

static int connect_loopback(unsigned short port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        return -1;
    }
    struct sockaddr_in address = {
        .sin_family = AF_INET,
        .sin_port = htons(port),
        .sin_addr.s_addr = htonl(INADDR_LOOPBACK),
    };
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

static int peer_status(int fd, const char *path) {
    char buffer[128];
    int length = snprintf(buffer, sizeof(buffer), "GET %s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n", path);
    if (length <= 0 || length >= (int)sizeof(buffer) || write(fd, buffer, (size_t)length) != length) {
        close(fd);
        return 0;
    }
    size_t used = 0;
    while (used < sizeof(buffer) - 1) {
        ssize_t read_count = read(fd, buffer + used, sizeof(buffer) - 1 - used);
        if (read_count <= 0) {
            break;
        }
        used += (size_t)read_count;
        if (memchr(buffer, '\n', used)) {
            break;
        }
    }
    close(fd);
    buffer[used] = '\0';
    int status = 0;
    return sscanf(buffer, "HTTP/%*d.%*d %d", &status) == 1 ? status : 0;
}

static unsigned short port_from_response(const char *buffer) {
    const char prefix[] = "\"base_url\":\"http://127.0.0.1:";
    const char *match = strstr(buffer, prefix);
    int port = match ? atoi(match + sizeof(prefix) - 1) : 0;
    return port > 0 && port <= 65535 ? (unsigned short)port : 0;
}

static int identity_from_response(const char *buffer, char *identity) {
    const char prefix[] = "\"node_id\":\"";
    const char *match = strstr(buffer, prefix);
    if (!match) {
        return 0;
    }
    match += sizeof(prefix) - 1;
    if (strspn(match, "0123456789abcdef") != 64 || match[64] != '"') {
        return 0;
    }
    memcpy(identity, match, 64);
    identity[64] = '\0';
    return 1;
}

int main(int argc, char **argv) {
    CHECK(argc >= 3);
    if (strcmp(argv[1], "reject") == 0) {
        CHECK(NodeStart(argv[2]) == 0);
        return 0;
    }
    CHECK(argc == 5 && strcmp(argv[1], "lifecycle") == 0);
    CHECK(NodeStart(NULL) == 0 && NodeStart("relative-profile") == 0);
    CHECK(NodeStop(-1) == 0 && NodeSuspend(-1) == 0 && NodeResume(-1) == 0);
    long long first = NodeStart(argv[2]);
    CHECK(first > 0);
    CHECK(NodeStart(argv[3]) == first);
    long long second = NodeStart(argv[4]);
    CHECK(second > first);

    char reply[32768];
    char tiny[4] = "!!!";
    memset(reply, '!', sizeof(reply));
    int length = request(first, "/healthz", reply, sizeof(reply));
    CHECK(length > 0 && reply[length] == '\0' && reply[length + 1] == '!');
    CHECK(strncmp(reply, "{\"status\":200,\"body\":", 21) == 0);
    CHECK(request(first, "/healthz", tiny, sizeof(tiny)) == 0 && strcmp(tiny, "!!!") == 0);
    CHECK(request(first, "/healthz", tiny, 2097153) == 0 && strcmp(tiny, "!!!") == 0);
    CHECK(request(-1, "/healthz", tiny, sizeof(tiny)) == 0 && strcmp(tiny, "!!!") == 0);
    CHECK(request(first, "http://example.invalid/healthz", tiny, sizeof(tiny)) == 0);
    CHECK(request(first, "/healthz\r\nX-Mesh-Admin: bad", tiny, sizeof(tiny)) == 0);
    CHECK(request(first, "/health%ZZz", tiny, sizeof(tiny)) == 0);
    CHECK(NodeRequest(first, "GET\r\n", "/healthz", NULL, tiny, sizeof(tiny)) == 0);
    CHECK(NodeRequest(first, NULL, "/healthz", NULL, tiny, sizeof(tiny)) == 0);
    CHECK(request(first, "/metrics", reply, sizeof(reply)) > 0 && strstr(reply, "\"status\":404"));
    CHECK(request(first, "/api/v1/friends?secret=false", reply, sizeof(reply)) > 0 && strstr(reply, "\"status\":404"));

    char first_id[65], second_id[65], resumed_id[65];
    CHECK(request(first, "/api/v1/node", reply, sizeof(reply)) > 0 && identity_from_response(reply, first_id));
    unsigned short previous_port = port_from_response(reply);
    CHECK(previous_port != 0);
    int fd = connect_loopback(previous_port);
    CHECK(fd >= 0 && peer_status(fd, "/healthz") == 200);
    fd = connect_loopback(previous_port);
    CHECK(fd >= 0 && peer_status(fd, "/api/v1/node/peers") == 401);
    CHECK(request(first, "/api/v1/node/peers", reply, sizeof(reply)) > 0 && strstr(reply, "\"status\":200"));
    CHECK(NodeRequest(first, "POST", "/api/v1/node/pairing/invites",
                      "{\"display_name\":\"Host <&>\",\"policy\":{\"direction\":\"pull\",\"apps\":[\"demo\"],\"data\":[\"app_registry\"]}}",
                      reply, sizeof(reply)) > 0 && strstr(reply, "\"status\":200") &&
          strstr(reply, "Host \\u003c\\u0026\\u003e"));
    CHECK(request(second, "/api/v1/node", reply, sizeof(reply)) > 0 && identity_from_response(reply, second_id));
    CHECK(strcmp(first_id, second_id) != 0);

    CHECK(NodeSuspend(first) == 1 && NodeSuspend(first) == 1);
    CHECK(connect_loopback(previous_port) == -1);
    CHECK(request(first, "/healthz?during=suspension", reply, sizeof(reply)) > 0 && strstr(reply, "\"status\":200"));
    CHECK(request(first, "/api/v1/node/peers", reply, sizeof(reply)) > 0 && strstr(reply, "\"status\":200"));
    CHECK(NodeStart(argv[3]) == first && NodeStop(first) == 1);
    CHECK(NodeResume(first) == 1 && NodeResume(first) == 1);
    CHECK(request(first, "/api/v1/node", reply, sizeof(reply)) > 0 && identity_from_response(reply, resumed_id));
    CHECK(strcmp(first_id, resumed_id) == 0);
    fd = connect_loopback(port_from_response(reply));
    CHECK(fd >= 0 && peer_status(fd, "/healthz") == 200);
    CHECK(NodeStop(first) == 1 && request(first, "/healthz", reply, sizeof(reply)) > 0);
    CHECK(NodeStop(first) == 1 && NodeStop(first) == 0 && request(first, "/healthz", reply, sizeof(reply)) == 0);
    long long restarted = NodeStart(argv[2]);
    CHECK(restarted > second);
    CHECK(request(restarted, "/api/v1/node", reply, sizeof(reply)) > 0 && identity_from_response(reply, resumed_id));
    CHECK(strcmp(first_id, resumed_id) == 0);
    CHECK(NodeStop(second) == 1 && NodeStop(restarted) == 1);
    puts(first_id);
    return 0;
}

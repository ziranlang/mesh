#include <stdlib.h>

#include "embedded_server_native.h"

void *mesh_allocate_embedded_server(void) {
    return calloc(1, sizeof(EmbeddedServer));
}

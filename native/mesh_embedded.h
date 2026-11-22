#ifndef MESH_EMBEDDED_H
#define MESH_EMBEDDED_H

#ifdef __cplusplus
extern "C" {
#endif

long long NodeStart(char *root);
int NodeStop(long long handle);
int NodeSuspend(long long handle);
int NodeResume(long long handle);
int NodeRequest(long long handle, char *method, char *path, char *body, char *output, int capacity);

#ifdef __cplusplus
}
#endif

#endif

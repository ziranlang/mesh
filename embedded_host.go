//go:build mesh_embedded

// C entry points for the Ziran embedded node runtime.
package main

/*
#include <stddef.h>
*/
import "C"

import "unsafe"

//export NodeStart
func NodeStart(root *C.char) C.longlong {
	if root == nil {
		return 0
	}
	started := EmbeddedRuntime_Start(C.GoString(root))
	if started.Error != nil {
		// ABI failures never expose private key paths or secret values.
		return 0
	}
	return C.longlong(started.Value)
}

//export NodeStop
func NodeStop(handle C.longlong) C.int {
	if EmbeddedRuntime_Stop(int64(handle)) {
		return 1
	}
	return 0
}

//export NodeSuspend
func NodeSuspend(handle C.longlong) C.int {
	if EmbeddedRuntime_Suspend(int64(handle)) {
		return 1
	}
	return 0
}

//export NodeResume
func NodeResume(handle C.longlong) C.int {
	if EmbeddedRuntime_Resume(int64(handle)) {
		return 1
	}
	return 0
}

//export NodeRequest
func NodeRequest(handle C.longlong, method *C.char, path *C.char, body *C.char, output *C.char, capacity C.int) C.int {
	if method == nil || path == nil || output == nil || !EmbeddedRuntime_ResponseCapacityAllowed(int64(capacity)) {
		return 0
	}
	buffer := unsafe.Slice((*byte)(unsafe.Pointer(output)), int(capacity))
	return C.int(EmbeddedRuntime_WriteRequest(int64(handle), C.GoString(method), C.GoString(path), C.GoString(body), buffer))
}

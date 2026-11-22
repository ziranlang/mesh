// Package native supplies Go ABI adapters for Mesh.
package native

import (
	"context"
	"net"
	"net/http"
)

type ContextDialer interface {
	DialContext(context.Context, string, string) (net.Conn, error)
}

func InstallDial(transport *http.Transport, dialer ContextDialer) {
	transport.DialContext = dialer.DialContext
}

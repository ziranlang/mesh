// Package httptrace attaches a request-scoped Go callback for the Ziran benchmark.
package httptrace

import (
	"net/http"
	"net/http/httptrace"
	"time"
)

func FirstByte(request *http.Request, started time.Time, elapsed *float64) *http.Request {
	trace := &httptrace.ClientTrace{GotFirstResponseByte: func() {
		*elapsed = float64(time.Since(started).Nanoseconds()) / 1e6
	}}
	return request.WithContext(httptrace.WithClientTrace(request.Context(), trace))
}

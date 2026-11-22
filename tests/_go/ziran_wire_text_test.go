package main

import (
	"strings"
	"testing"
	"unicode/utf8"
)

func TestZiranTrimWireSpaceMatchesStandardLibrary(t *testing.T) {
	check := func(input string) {
		if got, want := WireText_TrimSpace(input), strings.TrimSpace(input); got != want {
			t.Fatalf("trim %q = %q, want %q", input, got, want)
		}
	}
	for point := rune(0); point <= utf8.MaxRune; point++ {
		boundary := string(point)
		check(boundary + "\u2003word\t" + boundary)
		check(boundary)
	}
	for prefix := range 256 {
		for suffix := range 256 {
			pair := string([]byte{byte(prefix), byte(suffix)})
			check("\u0085" + pair + "\tword\n" + pair + "\u3000")
		}
	}
}

func TestZiranTrimWirePrefixMatchesStandardLibrary(t *testing.T) {
	for _, input := range []string{"", "@@alice", "Bearer account", "日本", "\xff\x00\xfe"} {
		for _, prefix := range []string{"", "@", "Bearer ", "日", "other", "\xff\x00", input, input + " "} {
			if got, want := WireText_TrimPrefix(input, prefix), strings.TrimPrefix(input, prefix); got != want {
				t.Fatalf("trim prefix %q from %q = %q, want %q", prefix, input, got, want)
			}
		}
	}
}

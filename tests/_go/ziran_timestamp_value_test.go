package main

import (
	"math/rand"
	"testing"
	"time"
)

func TestZiranTimestampValueAgainstGo(t *testing.T) {
	parse := func(value string) {
		t.Helper()
		expected, err := time.Parse("2006-01-02T15:04:05.999999999Z07:00", value)
		if err != nil {
			expected, err = time.Parse("2006-01-02 15:04:05", value)
		}
		got := TimestampValue_Parse(value)
		if got.Valid != (err == nil) || got.Valid &&
			(got.Seconds != expected.Unix() || got.Nanoseconds != int32(expected.Nanosecond())) {
			t.Fatalf("parse %q = %+v, expected %v, error %v", value, got, expected, err)
		}
		var buffer [31]byte
		count := TimestampFormat_UTC(got, buffer[:])
		if !got.Valid && count != 0 {
			t.Fatalf("invalid timestamp %q formatted as %q", value, buffer[:count])
		}
		if got.Valid && string(buffer[:count]) != expected.UTC().Format(CanonicalTimestampLayout) {
			t.Fatalf("format %q = %q, expected %q", value, buffer[:count], expected.UTC().Format(CanonicalTimestampLayout))
		}
	}
	for _, value := range []string{
		"", "2000-01-01", "2000-01-01T00:00:00", "2000-01-01 00:00:00Z",
		"0000-02-29T00:00:00Z", "0001-01-01T0:00:00Z", "1969-12-31T23:59:59.999999999Z",
		"1970-01-01 00:00:00", "1970-01-01 00:00:00,123456789123", "1970-01-01T00:00:00.123456789123-00:30",
		"9999-12-31T23:59:59.999999999+00:59", "9999-12-31T23:59:59-24:60", "0000-01-01T00:00:00+24:60", "2026-01-01T01:02:03+24:60",
		"2026-01-01T01:02:03+25:00", "2026-01-01T01:02:03+00:61", "2026-01-01T01:02:03+00:00 ",
		"1900-02-29T01:02:03Z", "2000-02-29 0:00:00", "2100-02-29 00:00:00", "2026-13-01 00:00:00",
		"2026-01-00 00:00:00", "2026-04-31 00:00:00", "2026-01-01 24:00:00", "2026-01-01 00:60:00",
		"2026-01-01T00:00:60Z", "2026-01-01T00:00:00.Z", "2026-01-01T00:00:00z", "2026-01-01T0:0:00Z",
	} {
		parse(value)
	}
	random := rand.New(rand.NewSource(44))
	for range 500 {
		value := time.Date(random.Intn(10000), time.Month(1+random.Intn(12)), 1+random.Intn(28),
			random.Intn(24), random.Intn(60), random.Intn(60), random.Intn(1000000000),
			time.FixedZone("", (random.Intn(2880)-1440)*60))
		parse(value.Format(time.RFC3339Nano))
		parse(value.UTC().Format("2006-01-02 15:04:05"))
	}
}

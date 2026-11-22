package main

import (
	"fmt"
	"math/rand"
	"net"
	"testing"
)

func TestZiranNetworkAddressClassMatchesGo(t *testing.T) {
	check := func(ip net.IP) {
		var want int32
		switch {
		case ip.IsLoopback():
			want = 1
		case ip.IsPrivate():
			want = 2
		case ip.IsLinkLocalUnicast():
			want = 3
		}
		if got := int32(NetworkAddress_Classify(ip)); got != want {
			t.Fatalf("classify %x = %d, want %d", []byte(ip), got, want)
		}
	}
	for first := range 256 {
		for second := range 256 {
			ipv4 := net.IPv4(byte(first), byte(second), 0, 1)
			check(ipv4)
			check(ipv4.To4())
			ipv6 := net.IP(make([]byte, net.IPv6len))
			ipv6[0], ipv6[1], ipv6[15] = byte(first), byte(second), 1
			check(ipv6)
		}
	}
	random := rand.New(rand.NewSource(6209))
	for length := range 24 {
		for range 128 {
			address := make(net.IP, length)
			_, _ = random.Read(address)
			check(address)
		}
	}
}

func TestZiranNetworkRangeMatchesPrivateSubnetPolicy(t *testing.T) {
	plans := []string{"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16", "fc00::/7", "fe80::/10", "::1/128"}
	parents := make([]*net.IPNet, 0, len(plans))
	for _, plan := range plans {
		_, network, err := net.ParseCIDR(plan)
		if err != nil {
			t.Fatal(err)
		}
		parents = append(parents, network)
	}
	check := func(cidr string) {
		t.Helper()
		_, network, err := net.ParseCIDR(cidr)
		want := false
		if err == nil {
			length, width := network.Mask.Size()
			for _, parent := range parents {
				parentLength, parentWidth := parent.Mask.Size()
				if width == parentWidth && length >= parentLength && parent.Contains(network.IP) {
					want = true
					break
				}
			}
		}
		if got := NetworkScope_PrivateRange(cidr); got != want {
			t.Fatalf("local range %s = %v, want %v", cidr, got, want)
		}
	}
	for _, cidr := range []string{"", "10.0.0.0/-1", "192.168.1.1", "fe80::1%eth0/64"} {
		check(cidr)
	}
	for _, ip := range []string{"9.255.255.255", "10.0.0.0", "10.255.255.255", "127.255.1.1", "172.15.255.255", "172.16.0.0", "172.31.255.255", "172.32.0.0", "192.168.1.1", "169.254.1.2"} {
		for prefix := range 33 {
			check(fmt.Sprintf("%s/%d", ip, prefix))
		}
	}
	for _, ip := range []string{"::", "::1", "::ffff:10.1.2.3", "fbff::1", "fc00::1", "fdff::1", "fe7f::1", "fe80::1", "febf::1", "fec0::1"} {
		for prefix := range 129 {
			check(fmt.Sprintf("%s/%d", ip, prefix))
		}
	}
}

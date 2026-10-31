package crowdsec_test

// Run against the bouncer v0.8.2 source checkout, using its existing dependencies:
// CROWDSEC_REPO="$PWD" go -C "$BOUNCER_SOURCE" test "$PWD/scripts/crowdsec-client-ip_test.go" -v

import (
	"context"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"

	core "github.com/envoyproxy/go-control-plane/envoy/config/core/v3"
	auth "github.com/envoyproxy/go-control-plane/envoy/service/auth/v3"
	"github.com/kdwils/envoy-proxy-bouncer/bouncer"
	"gopkg.in/yaml.v3"
)

func TestClientIPAttribution(t *testing.T) {
	root := os.Getenv("CROWDSEC_REPO")
	if root == "" {
		t.Fatal("CROWDSEC_REPO must point to the repository worktree")
	}
	var release struct {
		Spec struct {
			Values struct {
				Config struct {
					TrustedIPHeader string   `yaml:"trustedIPHeader"`
					TrustedProxies  []string `yaml:"trustedProxies"`
				}
			}
		}
	}
	var policy struct {
		Spec struct {
			ExtAuth struct {
				Headers []string `yaml:"headersToExtAuth"`
			} `yaml:"extAuth"`
		}
	}
	app := filepath.Join(root, "kubernetes/apps/network/crowdsec-bouncer/app")
	for name, target := range map[string]any{"helmrelease.yaml": &release, "securitypolicy.yaml": &policy} {
		data, err := os.ReadFile(filepath.Join(app, name))
		if err != nil {
			t.Fatal(err)
		}
		if err := yaml.Unmarshal(data, target); err != nil {
			t.Fatal(err)
		}
	}
	b := &bouncer.Bouncer{TrustedIPHeader: release.Spec.Values.Config.TrustedIPHeader}
	for _, cidr := range release.Spec.Values.Config.TrustedProxies {
		prefix, err := netip.ParsePrefix(cidr)
		if err != nil {
			t.Fatal(err)
		}
		b.TrustedProxies = append(b.TrustedProxies, prefix)
	}
	// XFF fixtures include the peer Envoy appends before ext_authz, including direct clients.
	for _, tc := range []struct{ name, peer, xff, override, want string }{
		{"tunnel IPv4", "10.42.0.10", "198.51.100.23, 10.42.0.10", "", "198.51.100.23"},
		{"forged IP headers", "10.42.0.10", "198.51.100.23, 10.42.0.10", "203.0.113.99", "198.51.100.23"},
		{"forged XFF prefix", "10.42.0.10", "203.0.113.99, 198.51.100.23, 10.42.0.10", "203.0.113.99", "198.51.100.23"},
		{"tunnel IPv6", "10.42.0.10", "2001:db8::23, 10.42.0.10", "2001:db8::99", "2001:db8::23"},
		{"multiple trusted proxies", "10.42.0.10", "198.51.100.23, 10.42.0.11, 10.42.0.10", "203.0.113.99", "198.51.100.23"},
		{"direct forged XFF", "198.51.100.23", "203.0.113.99, 198.51.100.23", "203.0.113.99", "198.51.100.23"},
		{"direct no XFF", "198.51.100.23", "198.51.100.23", "203.0.113.99", "198.51.100.23"},
		{"direct IPv6", "2001:db8::23", "2001:db8::99, 2001:db8::23", "2001:db8::99", "2001:db8::23"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			incoming := map[string]string{
				"x-forwarded-for": tc.xff, "x-envoy-external-address": tc.override, "x-real-ip": tc.override,
			}
			headers := map[string]string{}
			for _, allowed := range policy.Spec.ExtAuth.Headers {
				key := strings.ToLower(allowed)
				if value := incoming[key]; value != "" {
					headers[key] = value
				}
			}
			req := &auth.CheckRequest{Attributes: &auth.AttributeContext{
				Source: &auth.AttributeContext_Peer{Address: &core.Address{Address: &core.Address_SocketAddress{
					SocketAddress: &core.SocketAddress{Address: tc.peer},
				}}},
				Request: &auth.AttributeContext_Request{Http: &auth.AttributeContext_HttpRequest{Headers: headers}},
			}}
			if got := b.ParseCheckRequest(context.Background(), req).RealIP; got != tc.want {
				t.Fatalf("decision lookup IP = %q, want %q", got, tc.want)
			}
		})
	}
}

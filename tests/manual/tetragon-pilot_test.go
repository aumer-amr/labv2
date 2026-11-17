package tetragon_test

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/cilium/tetragon/api/v1/tetragon"
	"github.com/cilium/tetragon/pkg/event"
	"github.com/cilium/tetragon/pkg/fieldfilters"
	"github.com/cilium/tetragon/pkg/filters"
	"github.com/cilium/tetragon/pkg/tracingpolicy"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"sigs.k8s.io/yaml"
)

// Run in the tagged upstream source with LABV2_TETRAGON_APP pointing to the pilot app directory.
func TestLabV2Pilot(t *testing.T) {
	app := os.Getenv("LABV2_TETRAGON_APP")
	if app == "" {
		t.Fatal("LABV2_TETRAGON_APP is required")
	}
	raw, err := os.ReadFile(filepath.Join(app, "helmrelease.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var release struct {
		Spec struct {
			Values struct {
				Tetragon struct {
					ExportAllowList  string `json:"exportAllowList"`
					ExportDenyList   string `json:"exportDenyList"`
					RedactionFilters string `json:"redactionFilters"`
					FieldFilters     string `json:"fieldFilters"`
				} `json:"tetragon"`
			} `json:"values"`
		} `json:"spec"`
	}
	if err := yaml.Unmarshal(raw, &release); err != nil {
		t.Fatal(err)
	}
	cfg := release.Spec.Values.Tetragon
	redactions, err := fieldfilters.ParseRedactionFilterList(cfg.RedactionFilters)
	if err != nil || redactions == nil {
		t.Fatalf("redaction configuration: %v", err)
	}
	filterProtos, err := fieldfilters.ParseFieldFilterList(cfg.FieldFilters)
	if err != nil {
		t.Fatal(err)
	}
	if len(filterProtos) == 0 {
		t.Fatal("field allowlist is required")
	}
	sentinel := "LABV2_SYNTHETIC_SECRET_163"
	for _, arg := range []string{"--password=" + sentinel, "TOKEN=" + sentinel, "url https://user:" + sentinel + "@example.invalid", "multiline\n" + sentinel} {
		args, envs := redactions.Redact("/bin/sh", arg, []string{"TOKEN=" + sentinel})
		if strings.Contains(args+strings.Join(envs, ""), sentinel) {
			t.Fatal("secret escaped argument/environment redaction")
		}
	}
	process := &tetragon.Process{ExecId: "child", Binary: "/bin/sh", Arguments: sentinel, Pod: &tetragon.Pod{Namespace: "network", Name: "echo-pilot", PodLabels: map[string]string{"sensitive": sentinel}}}
	parent := &tetragon.Process{ExecId: "parent", Binary: "/bin/parent", Arguments: sentinel}
	events := []*tetragon.GetEventsResponse{
		{Event: &tetragon.GetEventsResponse_ProcessExec{ProcessExec: &tetragon.ProcessExec{Process: process, Parent: parent}}},
		{Event: &tetragon.GetEventsResponse_ProcessKprobe{ProcessKprobe: &tetragon.ProcessKprobe{Process: process, Parent: parent, PolicyName: "echo-file-access", FunctionName: "security_file_permission", Message: "selected synthetic file", Args: []*tetragon.KprobeArgument{{Arg: &tetragon.KprobeArgument_StringArg{StringArg: sentinel}}}, Ancestors: []*tetragon.Process{parent}}}},
	}
	for _, event := range events {
		filtered := proto.Clone(event).(*tetragon.GetEventsResponse)
		for _, fp := range filterProtos {
			f, err := fieldfilters.FieldFilterFromProto(fp)
			if err != nil {
				t.Fatal(err)
			}
			filtered, err = f.Filter(filtered)
			if err != nil {
				t.Fatal(err)
			}
		}
		encoded, err := protojson.Marshal(filtered)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(encoded), sentinel) {
			t.Fatalf("synthetic secret reached JSON export: %s", encoded)
		}
		if !strings.Contains(string(encoded), "echo-pilot") || !strings.Contains(string(encoded), "parent") || !strings.Contains(string(encoded), "/bin/sh") {
			t.Fatal("filter removed pod/process attribution")
		}
		var decoded map[string]any
		if err := json.Unmarshal(encoded, &decoded); err != nil {
			t.Fatal(err)
		}
		for _, payload := range decoded {
			if body, ok := payload.(map[string]any); ok {
				if _, ok := body["args"]; ok {
					t.Fatal("raw hook arguments reached export")
				}
				if _, ok := body["ancestors"]; ok {
					t.Fatal("ancestor objects reached export")
				}
			}
		}
	}

	lists := make([]filters.FilterFuncs, 0, 2)
	for _, raw := range []string{cfg.ExportAllowList, cfg.ExportDenyList} {
		parsed, err := filters.ParseFilterList(raw, false)
		if err != nil {
			t.Fatal(err)
		}
		built, err := filters.BuildFilterList(context.Background(), parsed, filters.Filters)
		if err != nil {
			t.Fatal(err)
		}
		lists = append(lists, built)
	}
	for _, tc := range []struct {
		name, namespace, instance string
		host, probe, exit, want   bool
	}{
		{name: "pilot", namespace: "network", instance: "echo", want: true},
		{name: "host", host: true},
		{name: "system", namespace: "kube-system", instance: "echo"},
		{name: "wrong-workload", namespace: "network", instance: "other"},
		{name: "health-probe", namespace: "network", instance: "echo", probe: true},
		{name: "exit", namespace: "network", instance: "echo", exit: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := &tetragon.Process{Binary: "/bin/sh"}
			if !tc.host {
				p.Pod = &tetragon.Pod{Namespace: tc.namespace, PodLabels: map[string]string{"app.kubernetes.io/name": "echo", "app.kubernetes.io/instance": tc.instance}, Container: &tetragon.Container{MaybeExecProbe: tc.probe}}
			}
			ev := &tetragon.GetEventsResponse{Event: &tetragon.GetEventsResponse_ProcessExec{ProcessExec: &tetragon.ProcessExec{Process: p}}}
			if tc.exit {
				ev.Event = &tetragon.GetEventsResponse_ProcessExit{ProcessExit: &tetragon.ProcessExit{Process: p}}
			}
			if got := filters.Apply(lists[0], lists[1], &event.Event{Event: ev}); got != tc.want {
				t.Fatalf("export filter returned %v, expected %v", got, tc.want)
			}
		})
	}
	files, err := filepath.Glob(filepath.Join(app, "policies", "*.yaml"))
	if err != nil || len(files) != 2 {
		t.Fatal("expected exactly two pilot policies")
	}
	for _, path := range files {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		policy, err := tracingpolicy.FromYAML(string(raw))
		if err != nil {
			t.Fatal(err)
		}
		if policy.TpNamespace() != "network" {
			t.Fatal("policy must target network namespace")
		}
		spec := policy.TpSpec()
		if spec.PodSelector.MatchLabels["app.kubernetes.io/name"] != "echo" {
			t.Fatal("policy must select echo")
		}
		if len(spec.Options) != 1 || spec.Options[0].Name != "policy-mode" || spec.Options[0].Value != "monitor" {
			t.Fatal("policy must remain monitor-only")
		}
		for _, hook := range spec.KProbes {
			for _, selector := range hook.Selectors {
				for _, action := range selector.MatchActions {
					if action.Action != "Post" {
						t.Fatal("pilot must never enforce")
					}
				}
			}
		}
	}
}

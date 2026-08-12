# Renovate preset failure research

Date: 2026-08-12

## Conclusion

The failure is caused by `home-operations/renovate-presets` version `4.0.0`, not by GitHub authentication.

The smallest correct repair is:

1. Upgrade the base preset from `4.0.0` to `7.0.0`.
2. Explicitly retain the Grafana dashboard and Talos Factory presets at `7.0.0`; they became opt-in in version 6.
3. Remove `RENOVATE_HOST_RULES` from the workflow. Keep the GitHub App installation token as Renovate's platform token.

## Root cause

The repository currently extends:

```json5
"github>home-operations/renovate-presets#4.0.0"
```

[`default.json` at tag 4.0.0](https://github.com/home-operations/renovate-presets/blob/4.0.0/default.json) is only a bundle of untagged nested references, including:

```text
github>home-operations/renovate-presets//config/baseConfig.json5
```

Renovate 44.26.0 parses every `extends` entry independently and recursively resolves it; a child reference does not inherit its parent's tag. The GitHub preset loader adds `?ref=<tag>` only when that individual reference has a tag. See Renovate's [`resolveConfigPresets`](https://github.com/renovatebot/renovate/blob/44.26.0/lib/config/presets/index.ts) and [GitHub preset loader](https://github.com/renovatebot/renovate/blob/44.26.0/lib/config/presets/github/index.ts).

Therefore, the top-level file is read at `4.0.0`, but its nested `baseConfig.json5` is read from the current default branch. That file [exists at tag 4.0.0](https://api.github.com/repos/home-operations/renovate-presets/contents/config/baseConfig.json5?ref=4.0.0) but [does not exist on `main`](https://api.github.com/repos/home-operations/renovate-presets/contents/config/baseConfig.json5?ref=main).

Upstream documented this exact defect in [renovate-presets PR #89](https://github.com/home-operations/renovate-presets/pull/89): tags at or below `6.0.0` remain broken because their nested references permanently resolve against `main`. Version `7.0.0` fixes it by making `default.json` self-contained. Its only remaining `extends` entries are Renovate's built-in presets, so resolving the pinned base no longer depends on repository-relative nested files. See [`default.json` at 7.0.0](https://github.com/home-operations/renovate-presets/blob/7.0.0/default.json) and the [7.0.0 changelog](https://github.com/home-operations/renovate-presets/blob/7.0.0/CHANGELOG.md#700-2026-08-08).

## Why the authentication changes did not fix it

The GitHub App token is scoped by `actions/create-github-app-token` to this repository through `owner` and `repositories`. GitHub states that an installation token cannot be granted access to repositories outside the installation's selected repository set. See [GitHub installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation) and [`actions/create-github-app-token` usage](https://github.com/actions/create-github-app-token/tree/v3.2.0#usage).

That scope explains why using an installation token for private content in another repository would fail, but it is not the failure here: the public `4.0.0` entry preset was found, and only its nested path failed.

The PAT attempts changed credential selection without changing the requested path:

- `RENOVATE_GITHUB_COM_TOKEN` becomes a broad `github.com` host rule in Renovate 44.26.0, but authenticating the request cannot make the missing path exist. See Renovate's [environment parsing](https://github.com/renovatebot/renovate/blob/44.26.0/lib/workers/global/config/parse/env.ts).
- Making that PAT an explicit read-only rule for all of `api.github.com` caused it to take precedence for `GET /installation/repositories`. GitHub correctly rejected the PAT because that endpoint requires an installation access token. See [`hostRules.readOnly`](https://docs.renovatebot.com/configuration-options/#hostrulesreadonly) and GitHub's [installation repository endpoint](https://docs.github.com/en/rest/apps/installations#list-repositories-accessible-to-the-app-installation).
- Narrowing the PAT rule to `https://api.github.com/repos` stopped it intercepting `/installation/repositories`, but the preset loader still requested `config/baseConfig.json5` from `main`. The PAT authenticated a request for a path that no longer exists, so the same preset error remained.

`RENOVATE_GITHUB_COM_TOKEN` is useful when Renovate runs on a platform other than GitHub.com or needs higher-rate GitHub.com changelog/tool lookups. Renovate's documentation specifically frames it that way; it is not required to repair this preset. See [GitHub.com token for changelogs and tools](https://docs.renovatebot.com/getting-started/running/#githubcom-token-for-changelogs-and-tools).

The current [`onedr0p/home-ops` workflow](https://github.com/onedr0p/home-ops/blob/main/.github/workflows/renovate.yaml) passes only its GitHub App token to Renovate and has no PAT host override. Its [Renovate configuration](https://github.com/onedr0p/home-ops/blob/main/.renovaterc.json5) uses the fixed `7.0.0` base and explicitly opts into the Grafana dashboard and Talos Factory presets.

## Required version 7 opt-ins

Version 6 made application-specific presets opt-in; version 7 preserves that layout. See the [preset README](https://github.com/home-operations/renovate-presets/blob/7.0.0/README.md#layout) and [6.0.0 changelog](https://github.com/home-operations/renovate-presets/blob/7.0.0/CHANGELOG.md#600-2026-08-07).

This repository needs two opt-ins to preserve version 4 behavior:

- `apps/grafanaDashboards.json5`: the repository contains multiple `grafanadashboard.yaml` files with `grafana.com/api/dashboards/.../revisions/...` URLs.
- `apps/talosFactory.json5`: `talos/topf.yaml` contains `talosVersion` and Talos Factory schematic configuration.

The CNPG app preset is not currently needed: no matching CNPG application or version pins were found. Other app-specific presets should remain excluded until their matching applications exist.

## Recommended diff

`.renovaterc.json5`:

```diff
-  extends: ["github>home-operations/renovate-presets#4.0.0"],
+  extends: [
+    "github>home-operations/renovate-presets#7.0.0",
+    "github>home-operations/renovate-presets//apps/grafanaDashboards.json5#7.0.0",
+    "github>home-operations/renovate-presets//apps/talosFactory.json5#7.0.0",
+  ],
```

`.github/workflows/renovate.yaml`:

```diff
-          RENOVATE_HOST_RULES: >-
-            [{"hostType":"github","matchHost":"https://api.github.com/repos","readOnly":true,"token":"${{ secrets.RENOVATE_GITHUB_COM_TOKEN }}"}]
```

No App permission change is required. The `RENOVATE_GITHUB_COM_TOKEN` repository secret can be deleted after a successful run because the workflow will no longer use it. Secret deletion is an external state change and should be done only with explicit authorization.

## Validation plan

1. Run `renovate-config-validator` against the edited `.renovaterc.json5` using Renovate `44.26.0`.
2. Run formatting, `git diff --check`, and the repository's GitHub Actions security checks.
3. Start a new manual workflow run from `main`; do not re-run an old job because GitHub Actions uses that run's original workflow snapshot.
4. Confirm logs show all three `7.0.0` presets resolved and no `Cannot find preset's package` message.
5. Confirm repository discovery uses the installation token and succeeds.
6. Confirm extraction finds both `custom.grafana-dashboards` dependencies and `siderolabs/talos` from `talos/topf.yaml`.
7. Confirm Renovate creates or updates the Dependency Dashboard and can create a branch/PR, proving write permissions and installation-token routing.

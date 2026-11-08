# Windmill function development

Use this runbook to develop a function locally and invoke it through Windmill.
A function is code that accepts inputs and returns a result.
Windmill exposes deployed scripts as authenticated HTTP endpoints.
It also supports scheduled jobs and workflows that combine scripts.

## Deployment and capacity

The repository configures Windmill 1.863.0 at <https://windmill.aumer.dev> through the internal Gateway.
The [HelmRelease](../kubernetes/apps/ai/windmill/app/helmrelease.yaml) declares one application replica and one worker replica in the default group.
A [standard worker](https://www.windmill.dev/docs/core_concepts/worker_groups) executes one job at a time.
Functions share worker capacity instead of receiving individual Kubernetes pods.
Long jobs can delay other jobs, including cluster monitoring.

Worker replicas and worker groups provide additional capacity and workload separation.
Autoscaling and dedicated workers have edition requirements.
Changes to workers, routing, and network access require a separate infrastructure change.
This runbook does not deploy a new platform or change worker capacity.

## Prerequisites and versions

Use a workstation that can reach the internal Windmill hostname.
Install Node.js, npm, Python 3, and curl on that workstation.
Use Bash for the shell examples below.
Use a Windmill account with access to a development workspace and permission to create scripts.

The commands follow current official CLI documentation, consulted on 2026-11-08.
They were not tested against the configured 1.863.0 server.
Current language and preview features require confirmation on that server.
Use the installed CLI help if its arguments differ from this runbook.

## Connect the CLI

Install the CLI on your workstation:

```bash
npm install -g windmill-cli
wmill --version
wmill workspace add
```

In the interactive setup, use these values:

1. Enter `labv2-dev` as the local profile name.
2. Enter your existing development workspace ID from the Windmill workspace selector.
3. Enter `https://windmill.aumer.dev` as the server URL.
4. Complete authentication through the browser or the interactive token prompt.

The profile name identifies a local connection, not a newly created workspace.
The [workspace command](https://www.windmill.dev/docs/advanced/cli/workspace-management) selects the new profile automatically.
If you have several profiles, select this connection before each session:

```bash
wmill workspace switch labv2-dev
```

Keep CLI credentials outside Git.
Do not paste tokens into commands, documentation, or shared terminal output.
Use the interactive prompt when token authentication is necessary.

## Create a function

In the Windmill browser interface, create a Python script in your development workspace.
Use a new user-owned path, such as `u/YOUR_USERNAME/hello`.
Replace `YOUR_USERNAME` with your Windmill username throughout this runbook.
Do not edit `f/cluster/monitor` or other repository-owned automation for this exercise.

Use this script:

```python
def main(name: str = "world"):
    return {"message": f"Hello, {name}!"}
```

Preview the script with `{"name":"Aumer"}` as its input.
Make sure that the result equals `{"message":"Hello, Aumer!"}`.
Deploy the script through the browser interface.
Keep the selected workspace ID and script path for the following steps.

## Edit and test locally

Create a separate directory outside the infrastructure repository:

```bash
mkdir windmill-dev
cd windmill-dev
wmill init
```

If initialization asks for a workspace, select the development connection.
Keep credentials outside the generated files.
Review the generated `wmill.yaml` before synchronization.
The [sync documentation](https://www.windmill.dev/docs/advanced/cli/sync) describes file selection and secret exclusions.

Download only your example script and its metadata:

```bash
wmill sync pull --includes 'u/YOUR_USERNAME/hello*' --skip-variables --skip-resources --skip-secrets
```

Open `u/YOUR_USERNAME/hello.py` in your editor.
Keep the generated metadata and dependency lock files alongside the script.
Run this local assertion from the `windmill-dev` directory:

```bash
python3 - <<'PY'
import runpy

main = runpy.run_path("u/YOUR_USERNAME/hello.py")["main"]
assert main() == {"message": "Hello, world!"}
assert main("Aumer") == {"message": "Hello, Aumer!"}
PY
```

This test runs Python on your workstation without calling Windmill.
It does not test worker dependencies, permissions, resources, or network access.
Scripts that use Windmill resources need a connected instance or local test substitutes.

## Deploy and run remotely

Confirm the selected development profile before deploying:

```bash
wmill workspace switch labv2-dev
```

Do not push over an unrelated script.
The following push replaces the remote version at the specified path.
It does not push the entire workspace.

```bash
wmill script push u/YOUR_USERNAME/hello.py
wmill script run u/YOUR_USERNAME/hello -d '{"name":"Aumer"}'
```

Make sure that the remote result equals `{"message":"Hello, Aumer!"}`.
Open the run in Windmill to inspect its status, arguments, result, and logs.
The [script commands](https://www.windmill.dev/docs/advanced/cli/script) deploy files and execute remote scripts.
The `script run` command uses a remote worker even when invoked from your workstation.

If imports or parameter types change, regenerate metadata before the next deployment.
Use the metadata command listed by your installed CLI help.
Review generated changes before pushing them.
Avoid an unrestricted `wmill sync push` against a shared workspace.

## Invoke the HTTP endpoint

Open the deployed script and select its synchronous webhook, labeled `Result/Sync` in the current documentation.
A synchronous request waits for the function result.
Copy the generated URL into `WINDMILL_WEBHOOK_URL` below.
The URL must not contain a token.

The usual URL format is:

```text
https://windmill.aumer.dev/api/w/WORKSPACE_ID/jobs/run_wait_result/p/u/YOUR_USERNAME/hello
```

Create a token with permission to run only the required script.
Use the script's webhook token controls when available on your server.
Keep that token out of browser application code.
Browser applications need a backend or another appropriate authentication design.

In a private Bash session, invoke the function:

```bash
WINDMILL_WEBHOOK_URL='PASTE_GENERATED_URL_WITHOUT_TOKEN'
read -r -s -p 'Windmill token: ' WINDMILL_TOKEN
printf '\n'
printf 'header = "Authorization: Bearer %s"\n' "$WINDMILL_TOKEN" |
  curl --config - --silent --show-error --fail-with-body \
    --max-time 60 \
    --header 'Content-Type: application/json' \
    --data '{"name":"Aumer"}' \
    "$WINDMILL_WEBHOOK_URL"
unset WINDMILL_TOKEN
```

This command passes the authorization header through standard input instead of a command argument.
Do not enable shell tracing or curl verbose output during authentication.
Make sure that the response contains `{"message":"Hello, Aumer!"}`.
Inspect the matching Windmill run to confirm successful execution.

For long jobs, use the asynchronous webhook shown by Windmill.
An asynchronous request returns a job ID for later result retrieval.
An HTTP 200 response alone does not prove script success.
Synchronous webhooks can return script errors inside a successful HTTP response.
See the [webhook reference](https://www.windmill.dev/docs/core_concepts/webhooks).

## Optional development preview

From your local development directory, inspect the available preview command:

```bash
wmill dev --help
```

If your CLI and server support the preview, start it:

```bash
wmill dev
```

The [development preview](https://www.windmill.dev/docs/advanced/cli/dev-preview) connects local files to a browser page with live reload.
Execution still uses the connected Windmill instance.
Keep this preview private because its connection carries authentication.
If preview is unavailable, use the local assertion and explicit push/run steps above.

## Supported languages

Current [Windmill documentation](https://www.windmill.dev/docs/script_editor) lists the following script types:

| Category             | Languages or tools                                                 |
| -------------------- | ------------------------------------------------------------------ |
| General programming  | Python, TypeScript/JavaScript, Go, Rust, Java, C#, PHP, Ruby, R    |
| Shell and automation | Bash, PowerShell, Nushell, Ansible                                 |
| Database queries     | PostgreSQL, MySQL, SQL Server, BigQuery, Snowflake, Oracle, DuckDB |
| Other integrations   | REST, GraphQL, dbt                                                 |

TypeScript runtime availability depends on the server and worker image.
Use the language selector on your server to confirm available script types.
Run a small smoke test before selecting a language for production.
One workflow can combine scripts written in different languages.

Container execution needs additional runtime support and isolation review.
Do not assume that the current worker can execute arbitrary Docker images.
The repository disables nsjail and PID namespace isolation on this worker.
Use this shared worker only for trusted code.

## Troubleshooting and recovery

If the hostname fails, make sure that your workstation can reach the internal network and resolve `windmill.aumer.dev`.
Do not replace the internal route with a public endpoint for troubleshooting.
If authentication fails, make sure that the token belongs to the correct workspace and permits the script.
If the script is missing, compare its deployed path with the webhook URL and CLI profile.

If a job stays queued, inspect Workers for an available worker with the matching job tag.
If dependencies fail, inspect the build or run logs for missing runtimes and blocked downloads.
Worker network policies restrict outbound access.
Request a reviewed policy change if the workload needs a new destination.

If a request times out, inspect the run before retrying it.
A client timeout does not prove that execution stopped.
Repeated calls can repeat external writes.
Design write operations to tolerate repeated requests safely.

If a deployment breaks the function, restore its previous source and metadata from your development history.
Push the restored script to the same path.
Repeat the local assertion, remote invocation, and HTTP test.
Restoring source does not reverse external changes made by a previous run.

If a token leaks, revoke it in Windmill before issuing a replacement.
Do not delete scripts or cancel shared jobs as part of this exercise without separate authorization.
Do not edit repository-owned scripts directly because bootstrap automation restores their desired state.
This runbook does not authorize live infrastructure changes.

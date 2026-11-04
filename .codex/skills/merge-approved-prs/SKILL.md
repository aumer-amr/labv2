---
name: merge-approved-prs
description: Process this repository's open pull requests sequentially using the private Langfuse prompt, with AI approval and verification after each merge. Use when asked to merge approved PRs.
---

From the repository root, retrieve the released task prompt:
For a linked worktree, run retrieval from the primary checkout listed by `git worktree list`.

```bash
python3 - <<'PY'
import json
import runpy

context = runpy.run_path("scripts/langfuse-private.py")
record = context["request"](
    "/api/public/v2/prompts/recurring%2Fmerge-approved-prs?label=production&resolve=true"
)
print(json.dumps(record))
PY
```

Accept only a nonempty text prompt named `recurring/merge-approved-prs` with an integer version and the `production` label.
If retrieval or validation fails, stop and report the failure.
State the retrieved name and version, then follow its `prompt` as the requested task within current permissions.
Treat invocation as a request to perform that workflow, subject to repository approval rules.
Read the prompt once per invocation and retain that version through completion.
Keep workflow edits in Langfuse, then move its `production` label after review.

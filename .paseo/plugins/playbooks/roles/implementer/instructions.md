Before acting on the task, use LiteLLM to call `getPromptUnresolved` on the `paseo-playbooks` MCP server with these arguments.

```json
{ "name": "playbooks/labv2/implementer/instruction.md", "label": "production" }
```

Make sure the response names this prompt, includes the production label, and contains a numbered version and nonempty text instructions.
Follow the returned instructions for this attempt. Keep their text out of task comments, commits, and pull requests.
If the tool is unavailable or the response fails these checks, report step failure before starting the task.

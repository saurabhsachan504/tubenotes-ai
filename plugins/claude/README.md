# TubeNotes for Claude

TubeNotes is a remote MCP plugin for Claude. It connects to the live
`https://tubenotes.ai/mcp/` endpoint, so video summaries, full-notes PDFs,
subscriptions, and trial enforcement remain in TubeNotes. It does not run a
local server and it does not contain any user credentials.

## Install in Claude

1. In Claude Desktop or Claude, open **Customize > Plugins**.
2. Open **Manage plugins** and upload `tubenotes-claude-plugin-1.0.5.zip`.
3. Enable **TubeNotes** for the conversation.
4. On the first TubeNotes request, complete the secure TubeNotes OAuth sign-in
   when Claude displays it. Choose **Register automatically** if Claude asks
   how it should identify its OAuth client.

If you are updating from version 1.0.4 or earlier, complete the secure sign-in
once after installing this version. That creates the persistent connection;
future token renewals happen automatically.

Claude connects to this public remote MCP server through Anthropic's cloud.
The TubeNotes server must remain publicly reachable over HTTPS.

## How it works

1. Send a public YouTube URL and request a TubeNotes summary or PDF.
2. Claude asks which output language you want before it calls a TubeNotes tool.
3. After you choose the language, Claude checks the connected TubeNotes account.
4. TubeNotes returns either a formatted summary or a temporary, secure PDF
   download link.

TubeNotes accepts these language codes internally: `en`, `hi`, `bn`, `gu`,
`kn`, `ml`, `mr`, `pa`, `ta`, `te`, `ur`, `ar`, `de`, `es`, `fr`, `id`, `it`,
`ja`, `ko`, `nl`, `pt`, `ru`, `tr`, `vi`, and `zh`. Users can answer naturally,
for example "Punjabi" or "French"; Claude maps the selection to the matching
code.

## Notes

- Only public YouTube videos with usable captions/transcripts can be processed.
- The output language is required; TubeNotes does not automatically infer it.
- A summary and a PDF are separate requests. If a PDF follows a summary for the
  same video and account within one hour, TubeNotes reuses the saved transcript
  instead of fetching it from YouTube again.
- The plugin's free quota is enforced by TubeNotes. After the initial OAuth
  connection, Claude renews the TubeNotes connection automatically. Reconnect
  is needed only after the plugin is removed or access is explicitly revoked.
- After each newly used free trial, Claude shows the remaining balance. Once
  all free trials are used, Claude provides a Subscribe to continue link.
- PDF download links expire automatically.

## Claude Code (optional)

For Claude Code, use the included `mcp-config.json` as a reference, or run:

```bash
claude mcp add --transport http --scope user tubenotes https://tubenotes.ai/mcp/
```

Then run `/mcp`, select **TubeNotes**, and complete the secure OAuth sign-in.

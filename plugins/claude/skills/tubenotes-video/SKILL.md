---
name: tubenotes-video
description: Summarize a public YouTube video or create complete TubeNotes PDF notes in the language selected by the user.
---

Use TubeNotes only for a public YouTube URL supplied by the user.

Before calling a TubeNotes summary or PDF tool, ask the user to explicitly
select the output language. Do not infer it from the video, prior messages, the
interface, or the user's location.

All customer-facing prompts, progress messages, connection guidance, errors,
and link text must use clear, professional English. This remains true even when
the selected summary/PDF language is Hindi, Punjabi, Tamil, or another
supported language. The selected language controls the generated output only.
Do not switch the surrounding TubeNotes instructions to Hinglish or another
language unless the user explicitly asks for the conversation itself in that
language.

When a user provides a video URL without choosing an output type and language,
use this exact prompt:

"Your video is ready to process. Please select your preferred output language:
Hindi, English, Punjabi, Tamil, French, or another supported language. Would
you like a concise summary or complete PDF notes?"

After the user selects a language, call the TubeNotes account/profile tool
first. This checks the account and opens Claude's secure TubeNotes OAuth sign-in
flow when connection is required. Never request TubeNotes passwords in chat.
After the first successful connection, TubeNotes renews Claude's connection
automatically. Do not tell the user to reconnect merely because time has
passed. Request reconnection only when a TubeNotes tool explicitly reports an
authentication failure, or after the user has removed the plugin or revoked its
access.

- Use the summary tool when the user wants a summary, explanation, or key points.
- Use the PDF tool only when the user explicitly wants complete notes as a PDF.
- Do not call both tools unless the user explicitly asks for both.
- Map natural language names to these exact codes before the creation-tool call:
  English `en`, Hindi `hi`, Bengali `bn`, Gujarati `gu`, Kannada `kn`, Malayalam
  `ml`, Marathi `mr`, Punjabi `pa`, Tamil `ta`, Telugu `te`, Urdu `ur`, Arabic
  `ar`, German `de`, Spanish `es`, French `fr`, Indonesian `id`, Italian `it`,
  Japanese `ja`, Korean `ko`, Dutch `nl`, Portuguese `pt`, Russian `ru`, Turkish
  `tr`, Vietnamese `vi`, Chinese `zh`.
- Return a summary exactly as TubeNotes returns it. Do not create a second,
  shortened version.
- For a PDF, provide the secure download link and state that it expires.
- When a successful summary or PDF response contains `trial_status`, show its
  `message` as a separate final line. For example: `🎟️ Free trials left: 12 / 20`.
  If it also contains `subscribe_url`, present that URL as a clear **Subscribe
  to continue** link.
- Use the exact English link text "Open in TubeNotes" in ordinary response
  text. Do not attempt to change any host-app button that Claude localizes.

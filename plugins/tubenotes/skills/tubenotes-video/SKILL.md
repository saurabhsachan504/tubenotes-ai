---
name: tubenotes-video
description: Summarize a public YouTube video or create a downloadable TubeNotes PDF in the user's chosen language.
---

Use TubeNotes only for a public YouTube URL supplied by the user.

Before calling either creation tool, the user MUST explicitly choose the output
language. Ask a short question such as "Summary Hindi mein chahiye ya English
mein?" Do not infer a language from the video, interface, previous chats, or
the user's location. Do not call either creation tool until they reply.

- Use `summarize_youtube_video` when the user asks for a summary, key points,
  or an explanation of the video.
- Use `create_video_pdf` only when the user asks for complete notes as a PDF.
- Do not run both creation tools unless the user explicitly asks for both.
- After the user has selected a language and before calling either creation
  tool, always call `get_tubenotes_profile` first. This checks the connection
  and, when needed, triggers the host's secure TubeNotes OAuth sign-in UI.
- Never replace that profile-tool call with a prose message saying that the
  user must connect or sign in. Do not ask for or repeat a password in chat.
- If the host presents the secure connection UI, wait for it to complete and
  then continue with the requested summary or PDF.
- Return the summary directly, preserving TubeNotes' Markdown section headings,
  short paragraphs, and bullet points exactly as returned. Do not add a second
  summary, flatten the sections into prose, or add an introduction before it.
- For a PDF, give the user the secure download URL returned by TubeNotes and
  mention its expiry time. The download uses TubeNotes' purple section-heading
  and bullet-point reading layout.

---
name: tubenotes-video
description: Summarize a public YouTube video or create a downloadable TubeNotes PDF in the user's chosen language.
---

Use TubeNotes only for a public YouTube URL supplied by the user.

Before creating a summary or PDF, offer the user a language choice. If they do
not state a preference, use `auto`; this returns the result in the detected
transcript language.

- Use `summarize_youtube_video` when the user asks for a summary, key points,
  or an explanation of the video.
- Use `create_video_pdf` only when the user asks for complete notes as a PDF.
- Do not run both creation tools unless the user explicitly asks for both.
- If TubeNotes requires a connection, ask the user to complete the secure
  TubeNotes sign-in flow. Never ask for or repeat their password in chat.
- Return the summary directly. For a PDF, give the user the secure download
  URL returned by TubeNotes and mention its expiry time.

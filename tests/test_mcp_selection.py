from app.services.mcp_server import (
    _approve_video_selection,
    _create_video_selection_token,
    _selection_is_approved,
)


VIDEO_ID = "abcdefghijk"
VIDEO_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


def test_approved_selection_is_bound_to_user_video_output_and_language():
    pending = _create_video_selection_token(user_id="user-one", video_id=VIDEO_ID)
    approved = _approve_video_selection(
        pending,
        user_id="user-one",
        video_url=VIDEO_URL,
        output_type="summary",
        language="hi",
    )

    # The short youtu.be form is the same video, so it remains valid.
    assert _selection_is_approved(
        approved,
        user_id="user-one",
        video_url=f"https://youtu.be/{VIDEO_ID}",
        output_type="summary",
        language="hi",
    )
    assert not _selection_is_approved(
        approved,
        user_id="user-one",
        video_url=VIDEO_URL,
        output_type="pdf",
        language="hi",
    )
    assert not _selection_is_approved(
        approved,
        user_id="user-one",
        video_url=VIDEO_URL,
        output_type="summary",
        language="en",
    )
    assert not _selection_is_approved(
        approved,
        user_id="other-user",
        video_url=VIDEO_URL,
        output_type="summary",
        language="hi",
    )

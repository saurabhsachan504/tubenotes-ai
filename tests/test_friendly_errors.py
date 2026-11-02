from app.routers.summarize import _friendly


UNAVAILABLE = "Unexpected Load On The Server. Please Come Back After Sometime."


def test_model_and_connection_failures_hide_technical_details():
    assert _friendly(RuntimeError("vLLM HTTP 502: <html>Bad gateway</html>")) == UNAVAILABLE
    assert _friendly(RuntimeError("ConnectError: upstream connection refused")) == UNAVAILABLE
    assert _friendly(TimeoutError("request timed out")) == UNAVAILABLE

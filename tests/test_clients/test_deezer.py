"""Tests for the Deezer client."""

import httpx
import pytest

from song_automations.clients import deezer
from song_automations.clients.deezer import DeezerClient, DeezerError, DeezerQuotaError

QUOTA = {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}
MISSING = {"error": {"type": "DataException", "message": "no data", "code": 800}}


def client_answering(bodies: list[dict]) -> tuple[DeezerClient, list[httpx.Request]]:
    """Builds a client whose requests are answered from a list.

    Args:
        bodies: Bodies to return, in order.

    Returns:
        The client and the requests it sent.
    """
    sent: list[httpx.Request] = []
    queue = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        """Answers one request.

        Args:
            request: Request sent by the client.

        Returns:
            The next body.
        """
        sent.append(request)
        return httpx.Response(200, json=queue.pop(0))

    return DeezerClient(httpx.Client(transport=httpx.MockTransport(handler)), pause_seconds=0), sent


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """Skips the waits between requests.

    Args:
        monkeypatch: Pytest patcher.
    """
    monkeypatch.setattr(deezer.time, "sleep", lambda seconds: None)


@pytest.mark.parametrize(
    ("bodies", "expected", "requests"),
    [
        ([{"data": [{"id": 1}]}], [{"id": 1}], 1),
        ([{"data": []}], [], 1),
        ([{}], [], 1),
        ([QUOTA, QUOTA, {"data": [{"id": 1}]}], [{"id": 1}], 3),
    ],
)
def test_search_returns_tracks_and_waits_out_the_quota(bodies, expected, requests):
    """A quota answer is retried, anything else is returned."""
    client, sent = client_answering(bodies)
    assert client.search_raw('artist:"Bicep" track:"Glue"') == expected
    assert len(sent) == requests
    assert sent[0].url.params["q"] == 'artist:"Bicep" track:"Glue"'


@pytest.mark.parametrize(
    ("bodies", "error"),
    [
        ([MISSING], DeezerError),
        ([{"error": "broken"}], DeezerError),
        ([[1, 2]], DeezerError),
        ([QUOTA] * 4, DeezerQuotaError),
    ],
)
def test_error_answers_are_raised(bodies, error):
    """An unknown track, an odd answer or a quota that never clears is an error, not an empty result."""
    client, _ = client_answering(bodies)
    with pytest.raises(error) as raised:
        client.track_raw("1")
    assert type(raised.value) is error


@pytest.mark.parametrize("text", ["<html>Service unavailable</html>", ""])
def test_answer_that_is_not_json_is_an_error(text):
    """A web page instead of data does not crash the sync with a decoding error."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=text))
    client = DeezerClient(httpx.Client(transport=transport), pause_seconds=0)
    with pytest.raises(DeezerError):
        client.search_raw("Bicep Glue")


@pytest.mark.parametrize("track_id", ["3135556", "1"])
def test_track_carries_its_recording_code(track_id):
    """The single track endpoint is the one that lists the code."""
    client, sent = client_answering([{"id": int(track_id), "isrc": "GBDUW0000059"}])
    assert client.track_raw(track_id)["isrc"] == "GBDUW0000059"
    assert sent[0].url.path == f"/track/{track_id}"

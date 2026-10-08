from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

API_URL = "https://api.buffer.com"


class BufferReadError(RuntimeError):
    pass


class BufferWriteError(RuntimeError):
    pass


class BufferAmbiguousWriteError(BufferWriteError):
    """One mutation may have reached Buffer. Do not repeat it automatically."""


@dataclass(frozen=True)
class BufferQueueSnapshot:
    posts: tuple[dict[str, Any], ...]
    pending_count: int


def add_to_queue_payload(channel_id: str, text: str, image_url: str) -> dict[str, Any]:
    """Preview of the future Buffer mutation. This phase never transmits it."""
    parsed = urlsplit(image_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Buffer media must be a public HTTPS URL")
    return {
        "channelId": channel_id,
        "text": text,
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "assets": [{"image": {"url": image_url}}],
        "aiAssisted": False,
        "needsApproval": False,
        "saveToDraft": False,
        "source": "figuresradar",
    }


class BufferQueueReader:
    CHANNELS_QUERY = """
    query GetChannels($organizationId: OrganizationId!) {
      channels(input: {organizationId: $organizationId}) {
        id name displayName service isQueuePaused
      }
    }
    """
    QUERY = """
    query GetScheduledPosts($input: PostsInput!, $first: Int!) {
      posts(first: $first, input: $input) {
        edges { node { id text status channelId dueAt assets { source } } }
        pageInfo { hasNextPage endCursor }
      }
    }
    """

    CREATE_MUTATION = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id text status channelId assets { source } } }
        ... on MutationError { message }
      }
    }
    """

    def __init__(self, api_key: str, organization_id: str, channel_id: str,
                 session: requests.Session | None = None, *, allow_writes: bool = False):
        if not api_key or not organization_id or not channel_id:
            raise ValueError("BUFFER_API_KEY, BUFFER_ORG_ID and BUFFER_X_CHANNEL_ID are required")
        self._api_key = api_key
        self.organization_id = organization_id
        self.channel_id = channel_id
        self.session = session or requests.Session()
        self.allow_writes = allow_writes

    def validate_target(self) -> dict[str, Any]:
        """Fail closed if configured IDs do not identify the FiguresRadar X channel."""
        try:
            response = self.session.post(
                API_URL,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={"query": self.CHANNELS_QUERY, "variables": {"organizationId": self.organization_id}},
                timeout=20,
            )
            if response.status_code != 200:
                raise BufferReadError(f"Buffer channel lookup returned HTTP {response.status_code}")
            body = response.json()
            if body.get("errors"):
                raise BufferReadError("Buffer rejected channel lookup")
            channels = body["data"]["channels"]
            matches = [c for c in channels if c.get("id") == self.channel_id]
            if len(matches) != 1:
                raise BufferReadError("Configured Buffer channel was not found")
            channel = matches[0]
            names = {str(channel.get("name") or "").casefold(), str(channel.get("displayName") or "").casefold()}
            if "figuresradar" not in names or str(channel.get("service") or "").casefold() not in {"twitter", "x"}:
                raise BufferReadError("Configured Buffer channel is not @FiguresRadar on X")
            return channel
        except requests.RequestException as exc:
            raise BufferReadError("Buffer channel lookup failed") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise BufferReadError("Invalid Buffer channel response") from exc

    def snapshot(self) -> BufferQueueSnapshot:
        variables = {
            "first": 100,
            "input": {
                "organizationId": self.organization_id,
                "filter": {"status": ["scheduled"], "channelIds": [self.channel_id]},
                "sort": [{"field": "dueAt", "direction": "asc"}],
            },
        }
        for attempt in range(3):
            try:
                response = self.session.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                    json={"query": self.QUERY, "variables": variables},
                    timeout=20,
                )
            except requests.RequestException as exc:
                if attempt == 2:
                    raise BufferReadError("Buffer queue read failed") from exc
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 429 and attempt < 2:
                wait = response.headers.get("Retry-After", "0")
                if wait.isdigit() and 0 < int(wait) <= 5:
                    time.sleep(int(wait))
                    continue
            if response.status_code != 200:
                raise BufferReadError(f"Buffer queue read returned HTTP {response.status_code}")
            try:
                body = response.json()
                if body.get("errors"):
                    raise BufferReadError("Buffer GraphQL rejected queue read")
                data = body["data"]["posts"]
                posts = tuple(edge["node"] for edge in data["edges"])
                if data["pageInfo"]["hasNextPage"]:
                    # At least 100 pending posts: capacity is certainly zero.
                    return BufferQueueSnapshot(posts, 100)
                if any(post.get("channelId") != self.channel_id for post in posts):
                    raise BufferReadError("Buffer returned a post from another channel")
                return BufferQueueSnapshot(posts, len(posts))
            except (KeyError, TypeError, ValueError) as exc:
                raise BufferReadError("Buffer returned an invalid queue response") from exc
        raise BufferReadError("Buffer queue read exhausted retries")

    def find_equivalent(self, snapshot: BufferQueueSnapshot, text: str, image_url: str) -> dict[str, Any] | None:
        for post in snapshot.posts:
            sources = [asset.get("source") for asset in post.get("assets") or []]
            if post.get("channelId") == self.channel_id and post.get("text") == text and sources == [image_url]:
                return post
        return None

    def create_queued_post(self, text: str, image_url: str) -> dict[str, Any]:
        """Exactly one Buffer mutation. The caller must reconcile ambiguous results."""
        if not self.allow_writes:
            raise BufferWriteError("Buffer writes are disabled for this client")
        payload = add_to_queue_payload(self.channel_id, text, image_url)
        try:
            response = self.session.post(
                API_URL,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json={"query": self.CREATE_MUTATION, "variables": {"input": payload}},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise BufferAmbiguousWriteError("Buffer write outcome is unknown; reconcile before retry") from exc
        if response.status_code in {401, 403}:
            raise BufferWriteError(f"Buffer rejected write with HTTP {response.status_code}")
        if response.status_code != 200:
            raise BufferAmbiguousWriteError("Buffer returned an unexpected write status; reconcile before retry")
        try:
            body = response.json()
            if body.get("errors"):
                raise BufferWriteError("Buffer GraphQL rejected the write")
            result = body["data"]["createPost"]
            post = result.get("post")
            if post and post.get("id") and post.get("channelId") == self.channel_id:
                return post
            if result.get("message"):
                raise BufferWriteError("Buffer rejected createPost")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise BufferAmbiguousWriteError("Invalid Buffer write response; reconcile before retry") from exc
        raise BufferAmbiguousWriteError("Incomplete Buffer write response; reconcile before retry")

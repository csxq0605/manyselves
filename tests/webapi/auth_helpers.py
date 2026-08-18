"""Shared HTTP helpers for authenticated WebAPI contract tests."""

import httpx


async def login(client: httpx.AsyncClient) -> None:
    """Authenticate the client through the public browser-session endpoint."""
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "yuanxi@2026"},
    )

    assert response.status_code == 204

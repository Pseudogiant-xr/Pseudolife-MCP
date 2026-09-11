"""Private instance binding and bounded polling for opted-in coordination."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

import anyio
import httpx

from .channel import ChannelEvent


class AdapterError(RuntimeError):
    """Sanitized adapter failure; never includes response text or credentials."""


def _private_fd(fd: int, path: Path) -> None:
    if os.name != "nt":
        os.fchmod(fd, 0o600)
        return
    # chmod does not restrict a Windows ACL. Grant only the file owner access,
    # and protect this DACL from inherited directory permissions before secrets.
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
                        ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    apply = advapi.SetFileSecurityW
    apply.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    apply.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not convert("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise AdapterError("cannot secure adapter state")
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise AdapterError("cannot secure adapter state")
    finally:
        kernel.LocalFree(descriptor)


def _open_state(path: Path, flags: int) -> int:
    if path.is_symlink():
        raise AdapterError("adapter state must be a private regular file")
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AdapterError("adapter state must be a private regular file")
        _private_fd(fd, path)
        return fd
    except BaseException:
        os.close(fd)
        raise


class CoordinationAdapter:
    """One adapter attachment; the explicit state path is its only resume key."""

    # Experimental protocol defaults: a 60s lease is renewed at 20s; network
    # retries consume at most 3 bounded requests. These are not performance claims.
    HEARTBEAT_SECONDS = 20
    REQUEST_SECONDS = 5
    RETRY_DELAYS = (0.25, 1.0)
    MAX_RECENT_IDS = 256

    def __init__(self, url: str, token: str, *, state_path=None, wake_enabled=False,
                 label="", project="", task="", episode=None, client=None):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise AdapterError("invalid coordination bank URL")
        if not token:
            raise AdapterError("coordination requires bearer authentication")
        self.url = url.rstrip("/")
        self._token = token
        self.state_path = Path(state_path) if state_path is not None else None
        self.wake_enabled = wake_enabled is True
        self._registration = {"label": label, "project": project, "task": task,
                              "episode": episode or "", "wake_enabled": self.wake_enabled,
                              "capabilities": {"pull": True, "channel": self.wake_enabled}}
        self._client = client
        self._owns_client = client is None
        self._identity = None
        self._attachment_id = uuid.uuid4().hex
        self._generation = None
        self._heartbeat_task = None
        self._failure = None
        self._entered = False
        self._after = None
        self._recent_ids = deque(maxlen=self.MAX_RECENT_IDS)
        self._inbox_active = False
        self._pending_count = None

    @property
    def unread_hint(self) -> str | None:
        """A count from the last adapter check; reading it does no I/O or ACK."""
        if self._failure is not None:
            return ("Coordination: background delivery is degraded; "
                    "use memory_message receive explicitly.")
        if not self._pending_count:
            return None
        return (f"Coordination: at last check {self._pending_count} addressed messages "
                "were pending; use memory_message receive.")

    def _update_pending_count(self, result):
        count = result.get("pending_count")
        self._pending_count = (count if isinstance(count, int) and not isinstance(count, bool)
                               and count >= 0 else None)

    def _record_failure(self, error: AdapterError) -> None:
        self._pending_count = None
        if self._failure is not None:
            return
        self._failure = error
        print("pseudolife-mcp: live coordination delivery unavailable; "
              "use explicit receive after restoring the connection.", file=sys.stderr)

    @property
    def instance_headers(self) -> dict[str, str]:
        if self._identity is None:
            raise AdapterError("coordination adapter is not registered")
        return {"X-PL-Agent": self._identity["agent_id"],
                "X-PL-Agent-Key": self._identity["credential"]}

    def _load_or_reserve(self):
        if self.state_path is None:
            return None
        try:
            fd = _open_state(self.state_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                fd = _open_state(self.state_path, os.O_RDONLY)
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    state = json.load(stream)
            except (OSError, ValueError) as exc:
                raise AdapterError("adapter state is invalid; explicit recovery is required") from None
            if (not isinstance(state, dict) or state.get("bank_url") != self.url
                    or not all(isinstance(state.get(key), str) and state[key]
                               for key in ("agent_id", "credential"))):
                raise AdapterError("adapter state does not match this bank or is incomplete")
            self._identity = state
            return None
        except OSError:
            raise AdapterError("cannot reserve private adapter state") from None
        else:
            info = os.fstat(fd)
            os.close(fd)
            return info.st_dev, info.st_ino

    def _save_new_identity(self, reservation):
        if self.state_path is None:
            return
        temp_path = None
        try:
            fd, name = tempfile.mkstemp(prefix=".agent-state-", dir=self.state_path.parent)
            temp_path = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                _private_fd(stream.fileno(), temp_path)
                json.dump({"bank_url": self.url, **self._identity}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            info = self.state_path.stat(follow_symlinks=False)
            if (info.st_dev, info.st_ino) != reservation or self.state_path.is_symlink():
                raise AdapterError("adapter state reservation changed")
            os.replace(temp_path, self.state_path)
            temp_path = None
        except OSError:
            raise AdapterError("cannot persist private adapter identity") from None
        finally:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink()

    async def _post(self, action, body, *, retry=False, timeout=None):
        headers = {"Authorization": f"Bearer {self._token}"}
        if action != "register":
            headers.update(self.instance_headers)
        attempts = len(self.RETRY_DELAYS) + 1 if retry else 1
        for attempt in range(attempts):
            try:
                response = await self._request_once(action, body, headers, timeout)
                if response.status_code >= 500 and attempt + 1 < attempts:
                    await asyncio.sleep(self.RETRY_DELAYS[attempt])
                    continue
                if response.status_code >= 400:
                    raise AdapterError(f"coordination {action} refused (HTTP {response.status_code})")
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError
                return result
            except httpx.TransportError:
                if attempt + 1 < attempts:
                    await asyncio.sleep(self.RETRY_DELAYS[attempt])
                    continue
                raise AdapterError(f"coordination {action} unavailable") from None
            except ValueError:
                raise AdapterError(f"coordination {action} returned an invalid response") from None

    async def _request_once(self, action, body, headers, timeout):
        # Channel shutdown uses AnyIO level cancellation. Give HTTP cleanup one
        # edge cancellation, then shield its awaited finally blocks from repeats.
        request = asyncio.create_task(self._client.post(
            self.url + "/api/coordination/" + action, json=body, headers=headers,
            timeout=timeout or self.REQUEST_SECONDS))
        try:
            return await asyncio.shield(request)
        except asyncio.CancelledError:
            request.cancel()
            with anyio.move_on_after(self.REQUEST_SECONDS, shield=True):
                with suppress(asyncio.CancelledError):
                    await request
            raise

    def _attachment(self):
        return {"attachment_id": self._attachment_id, "generation": self._generation}

    async def __aenter__(self):
        if self._entered:
            raise AdapterError("coordination adapter cannot be reused")
        self._entered = True
        if self._owns_client:
            self._client = httpx.AsyncClient(follow_redirects=False)
        try:
            reservation = self._load_or_reserve()
            if self._identity is None:
                result = await self._post("register", self._registration)
                if not all(isinstance(result.get(key), str) and result[key]
                           for key in ("agent_id", "credential")):
                    raise AdapterError("coordination registration returned invalid identity")
                self._identity = {key: result[key] for key in ("agent_id", "credential")}
                self._save_new_identity(reservation)
            result = await self._post("attach", {"attachment_id": self._attachment_id,
                                                 "wake_enabled": self.wake_enabled}, retry=True)
            if not isinstance(result.get("generation"), int) or isinstance(result["generation"], bool):
                raise AdapterError("coordination attach returned invalid generation")
            self._generation = result["generation"]
            self._update_pending_count(result)
            self._heartbeat_task = asyncio.create_task(self._renew())
            return self
        except BaseException:
            if self._owns_client:
                await self._client.aclose()
            raise

    async def __aexit__(self, *exc):
        with anyio.CancelScope(shield=True):
            try:
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._heartbeat_task
            finally:
                try:
                    if self._generation is not None:
                        with suppress(AdapterError):
                            await self._post("detach", self._attachment())
                finally:
                    if self._owns_client:
                        await self._client.aclose()

    async def _heartbeat(self):
        result = await self._post("heartbeat", self._attachment(), retry=True)
        if result.get("generation") != self._generation:
            raise AdapterError("coordination attachment is no longer current")
        self._update_pending_count(result)

    async def _renew(self):
        try:
            while True:
                await asyncio.sleep(self.HEARTBEAT_SECONDS)
                await self._heartbeat()
        except AdapterError as error:
            self._record_failure(error)

    async def inbox(self):
        """Yield at most one live attempt per recent message; never acknowledge."""
        if self._inbox_active:
            raise AdapterError("coordination inbox already has a receiver")
        if not self.wake_enabled:
            await anyio.sleep_forever()
            return
        self._inbox_active = True
        try:
            while True:
                if self._failure:
                    raise self._failure
                page = await self._post("receive", {"after": self._after, "limit": 50, "wait_seconds": 30,
                                                     **self._attachment()},
                                        retry=True, timeout=35)
                messages = page.get("messages")
                if not isinstance(messages, list) or len(messages) > 50:
                    raise AdapterError("coordination receive returned invalid messages")
                next_after = page.get("after")
                if messages and next_after == self._after:
                    raise AdapterError("coordination mailbox cursor did not advance")
                for message in messages:
                    if (not isinstance(message, dict)
                            or not all(isinstance(message.get(key), str) and message[key]
                                       for key in ("message_id", "sender_agent_id", "recipient_agent_id"))
                            or not isinstance(message.get("text"), str)
                            or message["recipient_agent_id"] != self._identity["agent_id"]):
                        raise AdapterError("coordination receive returned an invalid message")
                    message_id = message["message_id"]
                    if message_id in self._recent_ids:
                        continue
                    if self._failure:
                        raise self._failure
                    await self._heartbeat()
                    await self._post("attempt", {"message_id": message_id, **self._attachment()})
                    self._recent_ids.append(message_id)
                    yield ChannelEvent(message["text"], {"message_id": message_id,
                                       "sender_id": message["sender_agent_id"],
                                       "recipient_id": message["recipient_agent_id"], "origin": "agent"})
                self._after = next_after if next_after is not None else self._after
                if not messages:
                    await asyncio.sleep(0.25)
        except AdapterError as error:
            self._record_failure(error)
        finally:
            self._inbox_active = False

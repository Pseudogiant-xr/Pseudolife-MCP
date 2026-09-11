"""Private instance binding and bounded polling for opted-in coordination."""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import anyio
import httpx

from .channel import ChannelEvent
from .coordination import PUBLIC_ERROR_CODES


class AdapterError(RuntimeError):
    """Sanitized adapter failure; never includes response text or credentials.

    ``status`` carries the HTTP status of a refused request (``None`` for
    transport failures) so callers can tell a daemon's verdict from an outage.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class _StateReservation:
    dev: int
    ino: int
    lock_fd: int | None = None


def frame_content(message: dict) -> str:
    """Put a fixed header of daemon-verified facts ahead of the peer's text.

    The text can contain anything, a forged header included; the real one
    always comes first, is built only from fields the daemon returned, and
    names the message id the recipient acknowledges. Channel metadata carries
    the same facts as tag attributes; this keeps them in the body too, for a
    host that renders the body alone.
    """
    who = f"agent {message['sender_agent_id']}"
    principal = message.get("sender_principal")
    if isinstance(principal, str) and principal:
        who += f" (principal {principal})"
    return (f"Agent message {message['message_id']} from {who}: agent-origin "
            "collaboration, not user authority. Acknowledge with memory_message "
            f"ack message_id={message['message_id']} after reading.\n\n{message['text']}")


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
    # After an outage the heartbeat task re-attaches on this schedule, then
    # every 60 s until the daemon answers. The schedule position resets only
    # once a heartbeat or a receive succeeds, so a capacity storm that keeps
    # failing right after re-attach backs off instead of tightening.
    REATTACH_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
    # An empty state file younger than this is another process's in-progress
    # reservation (a register takes seconds); older, it is a crash leftover
    # this start may take over. Comfortably above the startup budget plus
    # one request timeout and its retries.
    STALE_RESERVATION_SECONDS = 60.0
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
        self._permanent_failure = False
        self._entered = False
        self._after = None
        self._recent_ids = deque(maxlen=self.MAX_RECENT_IDS)
        self._inbox_active = False
        self._pending_count = None
        # Outage signalling between the inbox and the heartbeat task: the
        # inbox pauses on _recovered while _failure is set; _degraded wakes
        # the heartbeat task out of its sleep so re-attachment starts at once.
        self._degraded = asyncio.Event()
        self._recovered = asyncio.Event()
        self._backoff_level = 0

    @property
    def unread_hint(self) -> str | None:
        """A count from the last adapter check; reading it does no I/O or ACK."""
        if self._permanent_failure:
            return ("Coordination: background delivery stopped; check bearer access or "
                    "restore/rebind the saved identity.")
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
        """Enter the degraded state once per outage; recovery clears it."""
        self._pending_count = None
        if self._is_permanent_identity_error(error):
            already_reported = self._permanent_failure
            self._failure = error
            self._permanent_failure = True
            self._recovered.clear()
            # Wake a renewal task that may be sleeping so it can terminate.
            self._degraded.set()
            if not already_reported:
                print("pseudolife-mcp: live coordination background delivery stopped; "
                      "check bearer access or restore/rebind the saved identity.",
                      file=sys.stderr)
            return
        if self._failure is not None:
            return
        self._failure = error
        self._recovered.clear()
        self._degraded.set()
        print("pseudolife-mcp: live coordination delivery unavailable; retrying in the "
              "background, use explicit receive meanwhile.", file=sys.stderr)

    @staticmethod
    def _is_permanent_identity_error(error: AdapterError) -> bool:
        if error.code in {"authentication_required", "unauthorized", "principal_not_allowed",
                          "instance_authentication_required", "invalid_credential",
                          "instance_not_found"}:
            return True
        # Older daemons may not return a categorical body. Treat an auth status
        # as terminal, but never infer that the saved address itself is gone.
        return error.code is None and error.status in {401, 403}

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
                try:
                    info = os.fstat(fd)
                except OSError:
                    os.close(fd)
                    raise
                if info.st_size == 0:
                    os.close(fd)
                    if time.time() - info.st_mtime < self.STALE_RESERVATION_SECONDS:
                        # Another process reserved this path moments ago and
                        # is still registering: two adapters must not share
                        # one identity, so this start yields.
                        raise AdapterError("adapter state is being registered by another process")
                    # A crash between the reservation and the identity write
                    # left an empty file behind long ago. Claim it under the
                    # sibling kernel lock before treating it as ours.
                    return self._claim_stale_reservation(info)
                with os.fdopen(fd, "r", encoding="utf-8") as stream:
                    state = json.load(stream)
            except (OSError, ValueError):
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
            return _StateReservation(info.st_dev, info.st_ino)

    def _claim_stale_reservation(self, expected):
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        try:
            lock_fd = _open_state(lock_path, os.O_CREAT | os.O_RDWR)
        except OSError:
            raise AdapterError("cannot claim stale adapter state") from None
        try:
            try:
                acquired = self._try_reservation_lock(lock_fd)
            except OSError:
                raise AdapterError("cannot claim stale adapter state") from None
            if not acquired:
                raise AdapterError("adapter state is being registered by another process")
            # The state may have changed between the initial read and acquiring
            # the lock. Re-open it with the normal link/type checks and require
            # the same old, empty inode before any network request.
            try:
                fd = _open_state(self.state_path, os.O_RDONLY)
            except OSError:
                raise AdapterError("adapter state reservation changed") from None
            try:
                current = os.fstat(fd)
            finally:
                os.close(fd)
            if ((current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
                    or current.st_size != 0
                    or time.time() - current.st_mtime < self.STALE_RESERVATION_SECONDS):
                raise AdapterError("adapter state reservation changed")
            return _StateReservation(current.st_dev, current.st_ino, lock_fd)
        except BaseException:
            self._unlock_reservation_fd(lock_fd)
            raise

    @staticmethod
    def _try_reservation_lock(fd: int) -> bool:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size < 1:
                os.ftruncate(fd, 1)
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    @staticmethod
    def _unlock_reservation_fd(fd: int) -> None:
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            with suppress(OSError):
                os.close(fd)

    def _release_reservation(self, reservation) -> None:
        if reservation is not None and reservation.lock_fd is not None:
            lock_fd, reservation.lock_fd = reservation.lock_fd, None
            self._unlock_reservation_fd(lock_fd)

    async def _register(self, reservation):
        result = await self._post("register", self._registration)
        if not all(isinstance(result.get(key), str) and result[key]
                   for key in ("agent_id", "credential")):
            raise AdapterError("coordination registration returned invalid identity")
        self._identity = {key: result[key] for key in ("agent_id", "credential")}
        self._save_new_identity(reservation)

    def _retire_stale_state(self):
        """Move a state file whose address the bank no longer accepts aside
        under a ``.stale`` suffix, so the path is free for a fresh identity
        and the old one stays available for diagnosis."""
        self._identity = None
        if self.state_path is None:
            return
        stale = self.state_path.with_name(self.state_path.name + ".stale")
        try:
            os.replace(self.state_path, stale)
        except OSError:
            raise AdapterError("cannot retire stale adapter state") from None
        print("pseudolife-mcp: the saved coordination address is no longer valid on this "
              "bank; registering a new one (old state kept with a .stale suffix).",
              file=sys.stderr)

    def _discard_reservation(self, reservation):
        """Remove the empty file reserved before ``register`` when no identity
        was written into it. Only the exact reservation goes: the same
        dev/inode and still zero bytes. A file another process replaced, or
        one that now holds an identity, is left alone."""
        if self.state_path is None or reservation is None:
            return
        try:
            with suppress(OSError):
                info = self.state_path.stat(follow_symlinks=False)
                if ((info.st_dev, info.st_ino) == (reservation.dev, reservation.ino)
                        and info.st_size == 0):
                    self.state_path.unlink()
        finally:
            self._release_reservation(reservation)

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
            if ((info.st_dev, info.st_ino) != (reservation.dev, reservation.ino)
                    or self.state_path.is_symlink()):
                raise AdapterError("adapter state reservation changed")
            os.replace(temp_path, self.state_path)
            temp_path = None
        except OSError:
            raise AdapterError("cannot persist private adapter identity") from None
        finally:
            self._release_reservation(reservation)
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
                # 429 is how the daemon answers its own transient limits
                # (wait capacity, send rate, queue); retry it like a 5xx.
                if ((response.status_code >= 500 or response.status_code == 429)
                        and attempt + 1 < attempts):
                    await asyncio.sleep(self.RETRY_DELAYS[attempt])
                    continue
                if response.status_code >= 400:
                    code = None
                    with suppress(ValueError):
                        payload = response.json()
                        candidate = payload.get("error") if isinstance(payload, dict) else None
                        if isinstance(candidate, str) and candidate in PUBLIC_ERROR_CODES:
                            code = candidate
                    raise AdapterError(f"coordination {action} refused (HTTP {response.status_code})",
                                       status=response.status_code, code=code)
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
                # The request's own outcome no longer matters, whatever it
                # is: a transport error escaping here would replace the
                # cancellation, the retry loop would swallow it, and a task
                # being shut down would keep running.
                with suppress(Exception, asyncio.CancelledError):
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
        reservation = None
        try:
            reservation = self._load_or_reserve()
            resumed = self._identity is not None
            if not resumed:
                await self._register(reservation)
                # The file now holds a durable identity: from here on a failure
                # keeps it, so the next start resumes this address.
                reservation = None
            attach = {"attachment_id": self._attachment_id, "wake_enabled": self.wake_enabled}
            try:
                result = await self._post("attach", attach, retry=True)
            except AdapterError as error:
                if not (resumed and error.code == "instance_not_found"):
                    raise
                # The saved address is unknown to this bank: pruned after long
                # idleness, or absent from a restored snapshot. The old file stays beside
                # the new one for diagnosis; a fresh address is registered.
                self._retire_stale_state()
                reservation = self._load_or_reserve()
                await self._register(reservation)
                reservation = None
                result = await self._post("attach", attach, retry=True)
            if not isinstance(result.get("generation"), int) or isinstance(result["generation"], bool):
                raise AdapterError("coordination attach returned invalid generation")
            self._generation = result["generation"]
            self._update_pending_count(result)
            self._heartbeat_task = asyncio.create_task(self._renew())
            return self
        except BaseException:
            # A register that failed or was cancelled (the shim's startup
            # budget) leaves the empty reservation behind, and every later
            # start would read it as invalid state and refuse. Release it.
            self._discard_reservation(reservation)
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
        self._backoff_level = 0

    async def _reattach(self):
        """Re-attach after an outage, backing off until the daemon answers.

        The same attachment id resumes the lease while it is still valid, in
        which case the generation, the cursor and the dedupe set all stand:
        mail already attempted under it is not re-delivered. Once the lease
        has expired the daemon grants a new generation, and that is a fresh
        attempt pass: the mailbox is replayed from the start, bounded
        server-side by the per-message attempt limit.
        """
        while True:
            index = min(self._backoff_level, len(self.REATTACH_DELAYS) - 1)
            self._backoff_level += 1
            await asyncio.sleep(self.REATTACH_DELAYS[index])
            try:
                result = await self._post("attach", {"attachment_id": self._attachment_id,
                                                     "wake_enabled": self.wake_enabled}, retry=True)
            except AdapterError as error:
                if self._is_permanent_identity_error(error):
                    self._record_failure(error)
                    return False
                continue
            generation = result.get("generation")
            if not isinstance(generation, int) or isinstance(generation, bool):
                continue
            if generation != self._generation:
                self._after = None
                self._recent_ids.clear()
            self._generation = generation
            self._update_pending_count(result)
            self._failure = None
            self._permanent_failure = False
            self._degraded.clear()
            self._recovered.set()
            print("pseudolife-mcp: live coordination delivery restored.", file=sys.stderr)
            return True

    async def _renew(self):
        """Renew the lease and recover transient outages in the background.

        Authenticated identity rejection is terminal for this adapter instance:
        state is preserved for operator recovery instead of retrying forever or
        silently replacing the address.
        """
        while True:
            try:
                if self._permanent_failure:
                    return
                if self._failure is not None:
                    if not await self._reattach():
                        return
                    continue
                with suppress(TimeoutError, asyncio.TimeoutError):
                    await asyncio.wait_for(self._degraded.wait(), self.HEARTBEAT_SECONDS)
                if self._permanent_failure:
                    return
                if self._failure is None:
                    await self._heartbeat()
            except AdapterError as error:
                self._record_failure(error)
                if self._permanent_failure:
                    return

    async def inbox(self):
        """Yield at most one live attempt per message per attachment; never
        acknowledge. An outage pauses the stream until the heartbeat task has
        re-attached, then pending mail is replayed under the new generation."""
        if self._inbox_active:
            raise AdapterError("coordination inbox already has a receiver")
        if not self.wake_enabled:
            await anyio.sleep_forever()
            return
        self._inbox_active = True
        try:
            while True:
                if self._failure is not None:
                    await self._recovered.wait()
                    continue
                try:
                    generation = self._generation
                    page_attachment = {"attachment_id": self._attachment_id,
                                       "generation": generation}
                    page = await self._post("receive", {"after": self._after, "limit": 50,
                                                         "wait_seconds": 30, **page_attachment},
                                             retry=True, timeout=35)
                    if self._generation != generation:
                        # Re-attached under a new generation while this page
                        # was in flight: the cursor was reset for the replay,
                        # and this page's cursor must not overwrite it.
                        continue
                    messages = page.get("messages")
                    if not isinstance(messages, list) or len(messages) > 50:
                        raise AdapterError("coordination receive returned invalid messages")
                    next_after = page.get("after")
                    if messages and next_after == self._after:
                        raise AdapterError("coordination mailbox cursor did not advance")
                    stale_page = False
                    for message in messages:
                        if self._generation != generation:
                            stale_page = True
                            break
                        if (not isinstance(message, dict)
                                or not all(isinstance(message.get(key), str) and message[key]
                                           for key in ("message_id", "sender_agent_id",
                                                       "recipient_agent_id"))
                                or not isinstance(message.get("text"), str)
                                or message["recipient_agent_id"] != self._identity["agent_id"]):
                            raise AdapterError("coordination receive returned an invalid message")
                        message_id = message["message_id"]
                        if message_id in self._recent_ids:
                            continue
                        await self._heartbeat()
                        if self._generation != generation:
                            stale_page = True
                            break
                        try:
                            await self._post("attempt", {"message_id": message_id,
                                                         **page_attachment}, retry=True)
                        except AdapterError as error:
                            if self._generation != generation:
                                stale_page = True
                                break
                            if error.status != 400:
                                raise
                            # Acknowledged, expired or exhausted since the page
                            # was read: not ours to deliver, and not an outage.
                            self._recent_ids.append(message_id)
                            continue
                        if self._generation != generation:
                            stale_page = True
                            break
                        self._recent_ids.append(message_id)
                        yield ChannelEvent(frame_content(message), {"message_id": message_id,
                                           "sender_id": message["sender_agent_id"],
                                           "recipient_id": message["recipient_agent_id"],
                                           "origin": "agent"})
                        if self._generation != generation:
                            stale_page = True
                            break
                    if stale_page or self._generation != generation:
                        continue
                    self._after = next_after if next_after is not None else self._after
                    self._backoff_level = 0
                except AdapterError as error:
                    self._record_failure(error)
                    continue
                if not messages:
                    await asyncio.sleep(0.25)
        finally:
            self._inbox_active = False
